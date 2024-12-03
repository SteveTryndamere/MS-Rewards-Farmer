# type: ignore
import argparse
import csv
import json
import logging
import logging.config
import logging.handlers as handlers
import random
import re
import struct
import sys
import time
import traceback
from datetime import datetime
from enum import Enum, auto

import pandas as pd

from src import (
    Account,
    Browser,
    DailySet,
    Login,
    MorePromotions,
    PunchCards,
    ReadToEarn,
    Searches,
)
from src.browser import RemainingSearches
from src.loggingColoredFormatter import ColoredFormatter
from src.utils import Utils, check_completion, manage_running_status, parse_log

MAX_RERUNS = 3
CD_BETWEEN_ACCOUNT_RUNS = 37


def get_rerun_accounts():
    rerun_needed_accounts = []
    # determine which accounts need to be rerun based on the recent logs
    try:
        log_summary = parse_log()  # fetch the most recent log
        df_completion = check_completion(log_summary, ret_type="df")
        df_max_run = df_completion.loc[
            df_completion.groupby("account")["run_index"].idxmax()
        ].reset_index(drop=True)
        today = datetime.today().date()
        df_max_run["is_run_today"] = (
            df_max_run["ts_start"].apply(pd.Timestamp).dt.date == today
        )
        logged_accountnames = list(df_max_run["account"].unique())
        # print(logged_accountnames)
        # accounts need to be rerun if the last run was not today or if the last run was today but not completed
        rerun_needed_accountnames = df_max_run[
            (df_max_run["is_run_today"] == False)
            | (
                (df_max_run["is_run_today"] == True)
                & (df_max_run["overall_completion"] == False)
            )
        ]["account"].tolist()
        rerun_needed_accounts = [
            acc
            for acc in setupAccounts()
            if acc.username in rerun_needed_accountnames
            or acc.username not in logged_accountnames
        ]
        cur_reruns = df_max_run[df_max_run["is_run_today"]]["run_index"].max()
    except FileNotFoundError:  # no log file found
        rerun_needed_accounts = setupAccounts()  # rerun all available accounts
        cur_reruns = 0
    return cur_reruns, rerun_needed_accounts


def main(accounts=None):
    manage_running_status(action="set", value=True)

    args = argumentParser()
    Utils.args = args
    setupLogging()
    loadedAccounts = setupAccounts() if accounts is None else accounts

    # Load previous day's points data
    previous_points_data = load_previous_points_data()

    logging.info("Main Run Started")
    for accountIter, currentAccount in enumerate(loadedAccounts, 1):
        logging.info(
            f"[POINTS] Processing account {accountIter}/{len(loadedAccounts)}: {currentAccount.username}"
        )
        try:
            earned_points = executeBot(currentAccount, args)
        except Exception as e1:
            logging.error("", exc_info=True)
            Utils.sendNotification(
                f"⚠️ Error executing {currentAccount.username}, please check the log",
                traceback.format_exc(),
            )
            continue
        previous_points = previous_points_data.get(currentAccount.username, 0)

        # Calculate the difference in points from the prior day
        points_difference = earned_points - previous_points

        # Append the daily points and points difference to CSV and Excel
        log_daily_points_to_csv(earned_points, points_difference)

        # Update the previous day's points data
        previous_points_data[currentAccount.username] = earned_points

        logging.info(
            f"[POINTS] Data for '{currentAccount.username}' appended to the file."
        )

    # Save the current day's points data for the next day in the "logs" folder
    save_previous_points_data(previous_points_data)
    logging.info("[POINTS] Data saved for the next day.")
    logging.info("Main Run Ended")

    manage_running_status(action="set", value=False)


def log_daily_points_to_csv(earned_points, points_difference):
    logs_directory = Utils.getProjectRoot() / "logs"
    csv_filename = logs_directory / "points_data.csv"

    # Create a new row with the date, daily points, and points difference
    date = datetime.now().strftime("%Y-%m-%d")
    new_row = {
        "Date": date,
        "Earned Points": earned_points,
        "Points Difference": points_difference,
    }

    fieldnames = ["Date", "Earned Points", "Points Difference"]
    is_new_file = not csv_filename.exists()

    with open(csv_filename, mode="a", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)

        if is_new_file:
            writer.writeheader()

        writer.writerow(new_row)


def setupLogging():
    _format = "%(asctime)s [%(levelname)s] %(message)s"
    terminalHandler = logging.StreamHandler(sys.stdout)
    terminalHandler.setFormatter(ColoredFormatter(_format))

    logs_directory = Utils.getProjectRoot() / "logs"
    logs_directory.mkdir(parents=True, exist_ok=True)

    # so only our code is logged if level=logging.DEBUG or finer
    # if not working see https://stackoverflow.com/a/48891485/4164390
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": True,
        }
    )
    logging.basicConfig(
        level=logging.DEBUG,
        format=_format,
        handlers=[
            handlers.TimedRotatingFileHandler(
                logs_directory / "activity.log",
                when="midnight",
                interval=1,
                backupCount=2,
                encoding="utf-8",
            ),
            terminalHandler,
        ],
    )


def argumentParser() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MS Rewards Farmer")
    parser.add_argument(
        "-v", "--visible", action="store_true", help="Optional: Visible browser"
    )
    parser.add_argument(
        "-l", "--lang", type=str, default=None, help="Optional: Language (ex: en)"
    )
    parser.add_argument(
        "-g", "--geo", type=str, default=None, help="Optional: Geolocation (ex: US)"
    )
    parser.add_argument(
        "-p",
        "--proxy",
        type=str,
        default=None,
        help="Optional: Global Proxy (ex: http://user:pass@host:port)",
    )
    parser.add_argument(
        "-vn",
        "--verbosenotifs",
        action="store_true",
        help="Optional: Send all the logs to the notification service",
    )
    parser.add_argument(
        "-cv",
        "--chromeversion",
        type=int,
        default=None,
        help="Optional: Set fixed Chrome version (ex. 118)",
    )
    parser.add_argument(
        "-da",
        "--disable-apprise",
        action="store_true",
        help="Optional: Disable Apprise, overrides config.yaml, useful when developing",
    )
    parser.add_argument(
        "-t",
        "--searchtype",
        type=str,
        default=None,
        help="Optional: Set to only search in either desktop or mobile (ex: 'desktop' or 'mobile')",
    )
    return parser.parse_args()


def setupAccounts() -> list[Account]:
    """Sets up and validates a list of accounts loaded from 'accounts.json'."""

    def validEmail(email: str) -> bool:
        """Validate Email."""
        pattern = r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
        return bool(re.match(pattern, email))

    accountPath = Utils.getProjectRoot() / "accounts.json"
    if not accountPath.exists():
        accountPath.write_text(
            json.dumps(
                [{"username": "Your Email", "password": "Your Password"}], indent=4
            ),
            encoding="utf-8",
        )
        noAccountsNotice = """
    [ACCOUNT] Accounts credential file "accounts.json" not found.
    [ACCOUNT] A new file has been created, please edit with your credentials and save.
    """
        logging.warning(noAccountsNotice)
        exit(1)
    loadedAccounts: list[Account] = []
    for rawAccount in json.loads(accountPath.read_text(encoding="utf-8")):
        account: Account = Account(**rawAccount)
        if not validEmail(account.username):
            logging.warning(
                f"[CREDENTIALS] Invalid email: {account.username}, skipping this account"
            )
            continue
        loadedAccounts.append(account)
    random.shuffle(loadedAccounts)
    return loadedAccounts


class AppriseSummary(Enum):
    """
    configures how results are summarized via Apprise
    """

    ALWAYS = auto()
    """
    the default, as it was before, how many points were gained and goal percentage if set
    """
    ON_ERROR = auto()
    """
    only sends email if for some reason there's remaining searches 
    """
    NEVER = auto()
    """
    never send summary 
    """


def executeBot(currentAccount: Account, args: argparse.Namespace):
    logging.info(f"********************{currentAccount.username}********************")

    startingPoints: int | None = None
    accountPoints: int
    remainingSearches: RemainingSearches
    goalTitle: str
    goalPoints: int

    if args.searchtype in ("desktop", None):
        with Browser(mobile=False, account=currentAccount, args=args) as desktopBrowser:
            utils = desktopBrowser.utils
            Login(desktopBrowser, args).login()
            startingPoints = utils.getAccountPoints()
            logging.info(
                f"[POINTS] You have {utils.formatNumber(startingPoints)} points on your account"
            )
            # todo Combine these classes so main loop isn't duplicated
            DailySet(desktopBrowser).completeDailySet()
            PunchCards(desktopBrowser).completePunchCards()
            MorePromotions(desktopBrowser).completeMorePromotions()
            # VersusGame(desktopBrowser).completeVersusGame()

            with Searches(desktopBrowser) as searches:
                searches.bingSearches()

            goalPoints = utils.getGoalPoints()
            goalTitle = utils.getGoalTitle()

            remainingSearches = desktopBrowser.getRemainingSearches(
                desktopAndMobile=True
            )
            accountPoints = utils.getAccountPoints()

    if args.searchtype in ("mobile", None):
        with Browser(mobile=True, account=currentAccount, args=args) as mobileBrowser:
            utils = mobileBrowser.utils
            Login(mobileBrowser, args).login()
            if startingPoints is None:
                startingPoints = utils.getAccountPoints()
            ReadToEarn(mobileBrowser).completeReadToEarn()
            with Searches(mobileBrowser) as searches:
                searches.bingSearches()

            goalPoints = utils.getGoalPoints()
            goalTitle = utils.getGoalTitle()

            remainingSearches = mobileBrowser.getRemainingSearches(
                desktopAndMobile=True
            )
            accountPoints = utils.getAccountPoints()

    logging.info(
        f"[POINTS] You have earned {Utils.formatNumber(accountPoints - startingPoints)} points this run !"
    )
    logging.info(
        f"[POINTS] You are now at {Utils.formatNumber(accountPoints)} points !"
    )
    appriseSummary = AppriseSummary[
        Utils.loadConfig().get("apprise", {}).get("summary", AppriseSummary.ALWAYS.name)
    ]
    if appriseSummary == AppriseSummary.ALWAYS:
        goalStatus = ""
        if goalPoints > 0:
            logging.info(
                f"[POINTS] You are now at {(Utils.formatNumber((accountPoints / goalPoints) * 100))}% of your "
                f"goal ({goalTitle}) !"
            )
            goalStatus = (
                f"🎯 Goal reached: {(Utils.formatNumber((accountPoints / goalPoints) * 100))}%"
                f" ({goalTitle})"
            )

        Utils.sendNotification(
            "Daily Points Update",
            "\n".join(
                [
                    f"👤 Account: {currentAccount.username}",
                    f"⭐️ Points earned today: {Utils.formatNumber(accountPoints - startingPoints)}",
                    f"💰 Total points: {Utils.formatNumber(accountPoints)}",
                    goalStatus,
                ]
            ),
        )
    elif appriseSummary == AppriseSummary.ON_ERROR:
        if remainingSearches.getTotal() > 0:
            Utils.sendNotification(
                "Error: remaining searches",
                f"account username: {currentAccount.username}, {remainingSearches}",
            )
    elif appriseSummary == AppriseSummary.NEVER:
        pass

    return accountPoints


def export_points_to_csv(points_data):
    logs_directory = Utils.getProjectRoot() / "logs"
    csv_filename = logs_directory / "points_data.csv"
    with open(csv_filename, mode="a", newline="") as file:  # Use "a" mode for append
        fieldnames = ["Account", "Earned Points", "Points Difference"]
        writer = csv.DictWriter(file, fieldnames=fieldnames)

        # Check if the file is empty, and if so, write the header row
        if file.tell() == 0:
            writer.writeheader()

        for data in points_data:
            writer.writerow(data)


# Define a function to load the previous day's points data from a file in the "logs" folder
def load_previous_points_data():
    try:
        with open(
            Utils.getProjectRoot() / "logs" / "previous_points_data.json", "r"
        ) as file:
            return json.load(file)
    except FileNotFoundError:
        return {}


# Define a function to save the current day's points data for the next day in the "logs" folder
def save_previous_points_data(data):
    logs_directory = Utils.getProjectRoot() / "logs"
    with open(logs_directory / "previous_points_data.json", "w") as file:
        json.dump(data, file, indent=4)


if __name__ == "__main__":
    # cur_reruns, rerun_needed_accounts = get_rerun_accounts()
    # max_runs_flag = cur_reruns < MAX_RERUNS
    # print(
    #     f"*** Today's runs done:{cur_reruns}, {len(rerun_needed_accounts)} accounts to rerun:{[a.username for a in rerun_needed_accounts]}"
    # )
    # while max_runs_flag and len(rerun_needed_accounts) > 0:
    #     print(
    #         f"*** Today's runs done:{cur_reruns}, {len(rerun_needed_accounts)} accounts to rerun:{[a.username for a in rerun_needed_accounts]}"
    #     )
    #     try:
    #         for account in rerun_needed_accounts:
    #             main([account])
    #             time.sleep(CD_BETWEEN_ACCOUNT_RUNS)
    #             logging.info(
    #                 f"*** sleeping for {CD_BETWEEN_ACCOUNT_RUNS} seconds before running the next account"
    #             )
    #     except Exception as e:
    #         # # TODO notify user with a message that the maximum number of reruns has been reached + df_completion
    #         # pass
    #         logging.exception("")
    #         Utils.sendNotification(
    #             "⚠️ Error occurred, please check the log", traceback.format_exc()
    #         )
    #     cur_reruns, rerun_needed_accounts = get_rerun_accounts()
    #     max_runs_flag = cur_reruns < MAX_RERUNS
    main()
