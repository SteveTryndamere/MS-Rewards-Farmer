# type: ignore
import contextlib
import json
import locale as pylocale
import logging
import re
import struct
import time
from argparse import Namespace
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import yaml
from apprise import Apprise
from requests import Session
from requests.adapters import HTTPAdapter
from selenium.common import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    NoSuchElementException,
    TimeoutException,
)
from selenium.webdriver.chrome.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions
from selenium.webdriver.support.wait import WebDriverWait
from tabulate import tabulate
from urllib3 import Retry

from .constants import REWARDS_URL, SEARCH_URL


class Utils:
    args: Namespace

    def __init__(self, webdriver: WebDriver):
        self.webdriver = webdriver
        with contextlib.suppress(Exception):
            locale = pylocale.getdefaultlocale()[0]
            pylocale.setlocale(pylocale.LC_NUMERIC, locale)

        self.config = self.loadConfig()

    @staticmethod
    def getProjectRoot() -> Path:
        return Path(__file__).parent.parent

    @staticmethod
    def loadConfig(configFilename="config.yaml") -> dict:
        configFile = Utils.getProjectRoot() / configFilename
        try:
            with open(configFile, "r") as file:
                config = yaml.safe_load(file)
                if not config:
                    logging.info(f"{file} doesn't exist")
                    return {}
                return config
        except OSError:
            logging.warning(f"{configFilename} doesn't exist")
            return {}

    @staticmethod
    def sendNotification(title, body) -> None:
        if Utils.args.disable_apprise:
            return None
        apprise = Apprise()
        urls: list[str] = (
            Utils.loadConfig("config-private.yaml").get("apprise", {}).get("urls", [])
        )
        if not urls:
            logging.debug("No urls found, not sending notification")
            return
        for url in urls:
            apprise.add(url)
        # assert apprise.notify(title=str(title), body=str(body)) # apprise sometimes return False even if notification is sent successfully
        apprise.notify(title=str(title), body=str(body))

    def waitUntilVisible(
        self, by: str, selector: str, timeToWait: float = 10
    ) -> WebElement:
        return WebDriverWait(self.webdriver, timeToWait).until(
            expected_conditions.visibility_of_element_located((by, selector))
        )

    def waitUntilClickable(
        self, by: str, selector: str, timeToWait: float = 10
    ) -> WebElement:
        return WebDriverWait(self.webdriver, timeToWait).until(
            expected_conditions.element_to_be_clickable((by, selector))
        )

    def checkIfTextPresentAfterDelay(self, text: str, timeToWait: float = 10) -> bool:
        time.sleep(timeToWait)
        text_found = re.search(text, self.webdriver.page_source)
        return text_found is not None

    def waitUntilQuestionRefresh(self) -> WebElement:
        return self.waitUntilVisible(By.CLASS_NAME, "rqECredits", timeToWait=20)

    def waitUntilQuizLoads(self) -> WebElement:
        return self.waitUntilVisible(By.XPATH, '//*[@id="rqStartQuiz"]')

    def resetTabs(self) -> None:
        curr = self.webdriver.current_window_handle

        for handle in self.webdriver.window_handles:
            if handle != curr:
                self.webdriver.switch_to.window(handle)
                time.sleep(0.5)
                self.webdriver.close()
                time.sleep(0.5)

        self.webdriver.switch_to.window(curr)
        time.sleep(0.5)
        self.goToRewards()

    def goToRewards(self) -> None:
        max_retries = 5
        retry_delay = 30
        retry_count = 0
        while retry_count < max_retries:
            try:
                self.webdriver.get(REWARDS_URL)
                assert (
                    self.webdriver.current_url == REWARDS_URL
                ), f"{self.webdriver.current_url} {REWARDS_URL}"
                break
            except TimeoutException:
                retry_count += 1
                logging.warning(
                    f"[LOGIN] Logged in but Dashboard not loaded, retrying ({retry_count}/{max_retries})..."
                )
                time.sleep(retry_delay)
        else:
            logging.error("[LOGIN] Failed to load Dashboard after maximum retries.")
            raise TimeoutException("Failed to load Dashboard after maximum retries.")

    def goToSearch(self) -> None:
        self.webdriver.get(SEARCH_URL)
        # max_retries = 5
        # retry_delay = 30
        # retry_count = 0
        # while retry_count < max_retries:
        #     try:
        #         self.webdriver.get(SEARCH_URL)
        #         # assert (
        #         #     self.webdriver.current_url == SEARCH_URL
        #         # ), f"{self.webdriver.current_url} {SEARCH_URL}"  # need regex: AssertionError: https://www.bing.com/?toWww=1&redig=A5B72363182B49DEBB7465AD7520FDAA https://bing.com/
        #     except TimeoutException:
        #         retry_count += 1
        #         logging.warning(
        #             f"[LOGIN] Logged in but Search Page not loaded, retrying ({retry_count}/{max_retries})..."
        #         )
        #         time.sleep(retry_delay)

    @staticmethod
    def getAnswerCode(key: str, string: str) -> str:
        t = sum(ord(string[i]) for i in range(len(string)))
        t += int(key[-2:], 16)
        return str(t)

    def getDashboardData(self) -> dict:
        urlBefore = self.webdriver.current_url
        try:
            self.goToRewards()
            return self.webdriver.execute_script("return dashboard")
        finally:
            try:
                self.webdriver.get(urlBefore)
            except TimeoutException:
                self.goToRewards()

    def getBingInfo(self) -> Any:
        session = self.makeRequestsSession()

        for cookie in self.webdriver.get_cookies():
            session.cookies.set(cookie["name"], cookie["value"])

        response = session.get("https://www.bing.com/rewards/panelflyout/getuserinfo")

        assert response.status_code == requests.codes.ok
        return response.json()["userInfo"]

    @staticmethod
    def makeRequestsSession(session: Session = requests.session()) -> Session:
        retry = Retry(total=5, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
        session.mount(
            "https://", HTTPAdapter(max_retries=retry)
        )  # See https://stackoverflow.com/a/35504626/4164390 to finetune
        session.mount(
            "http://", HTTPAdapter(max_retries=retry)
        )  # See https://stackoverflow.com/a/35504626/4164390 to finetune
        return session

    def isLoggedIn(self) -> bool:
        if self.getBingInfo()["isRewardsUser"]:  # faster, if it works
            return True
        self.webdriver.get(
            "https://rewards.bing.com/Signin/"
        )  # changed site to allow bypassing when M$ blocks access to login.live.com randomly

        with contextlib.suppress(TimeoutException):
            self.waitUntilVisible(
                By.CSS_SELECTOR, 'html[data-role-name="RewardsPortal"]', 10
            )
            return True
        return False

    def getAccountPoints(self) -> int:
        return self.getBingInfo()["balance"]

    def getGoalPoints(self) -> int:
        return self.getDashboardData()["userStatus"]["redeemGoal"]["price"]

    def getGoalTitle(self) -> str:
        return self.getDashboardData()["userStatus"]["redeemGoal"]["title"]

    def tryDismissAllMessages(self) -> None:
        buttons = [
            (By.ID, "iLandingViewAction"),
            (By.ID, "iShowSkip"),
            (By.ID, "iNext"),
            (By.ID, "iLooksGood"),
            (By.ID, "idSIButton9"),
            (By.ID, "bnp_btn_accept"),
            (By.ID, "acceptButton"),
        ]
        for button in buttons:
            try:
                elements = self.webdriver.find_elements(by=button[0], value=button[1])
            except (
                NoSuchElementException,
                ElementNotInteractableException,
            ):  # Expected?
                logging.debug("", exc_info=True)
                continue
            for element in elements:
                element.click()
        self.tryDismissCookieBanner()
        self.tryDismissBingCookieBanner()

    def tryDismissCookieBanner(self) -> None:
        with contextlib.suppress(
            NoSuchElementException, ElementNotInteractableException
        ):  # Expected
            self.webdriver.find_element(By.ID, "cookie-banner").find_element(
                By.TAG_NAME, "button"
            ).click()

    def tryDismissBingCookieBanner(self) -> None:
        with contextlib.suppress(
            NoSuchElementException, ElementNotInteractableException
        ):  # Expected
            self.webdriver.find_element(By.ID, "bnp_btn_accept").click()

    def switchToNewTab(self, timeToWait: float = 0) -> None:
        self.webdriver.switch_to.window(window_name=self.webdriver.window_handles[1])

    def closeCurrentTab(self) -> None:
        self.webdriver.close()
        time.sleep(0.5)
        self.webdriver.switch_to.window(window_name=self.webdriver.window_handles[0])
        time.sleep(0.5)

    def visitNewTab(self, timeToWait: float = 0) -> None:
        self.switchToNewTab(timeToWait)
        self.closeCurrentTab()

    @staticmethod
    def formatNumber(number, num_decimals=2) -> str:
        return pylocale.format_string(
            f"%10.{num_decimals}f", number, grouping=True
        ).strip()

    @staticmethod
    def getBrowserConfig(sessionPath: Path) -> dict | None:
        configFile = sessionPath / "config.json"
        if not configFile.exists():
            return None
        with open(configFile, "r") as f:
            return json.load(f)

    @staticmethod
    def saveBrowserConfig(sessionPath: Path, config: dict) -> None:
        configFile = sessionPath / "config.json"
        with open(configFile, "w") as f:
            json.dump(config, f)

    def click(self, element: WebElement) -> None:
        try:
            element.click()
        except (ElementClickInterceptedException, ElementNotInteractableException):
            self.tryDismissAllMessages()
            element.click()


########NOTE:  below are components from to parse log file and check completion status.
# Note that we have addded some new patterns to the origin logging for easier parsing.

logs_directory = Utils.getProjectRoot() / "logs"
DEFUALT_LOG_FILE = logs_directory / "activity.log"
STATUS_FILE = logs_directory / "running_status.bin"


def manage_running_status(method: str, value: bool | None = None) -> bool | None:
    """
    Updates or reads the running status from the binary file.
    :param method: 'get' to read the status, 'set' to update the status.
    :param is_running: The running status to set if method is 'set'.
    :return: The running status if method is 'get'.
    """
    if method == "set":
        if value is None:
            raise ValueError("value must be provided when method is 'set'")
        else:
            assert isinstance(value, bool), "value must be a boolean when method='set'"
        with open(STATUS_FILE, "wb") as file:
            file.write(struct.pack("?", value))  # Write a single boolean value
        file.close()
        return None
    elif method == "get":
        try:
            with open(STATUS_FILE, "rb") as file:
                output = struct.unpack("?", file.read(1))[
                    0
                ]  # Read a single boolean value
            file.close()
            return output
        except FileNotFoundError:
            # If the status file doesn't exist, assume not running
            return False
    elif method == "reset":
        if STATUS_FILE.exists():
            STATUS_FILE.unlink()
        return None
    else:
        raise ValueError("method must be either 'get' or 'set'")


# def parse_log(log_file_path=None):
#     """Parse the log file for task completion metadata."""
#     log_file_path = log_file_path if log_file_path else DEFUALT_LOG_FILE
#     log_file = Path(log_file_path)

#     if log_file.exists():
#         with open(log_file, "r") as file:
#             log_lines = file.readlines()
#         file.close()
#     else:
#         raise FileNotFoundError(f"Log file {log_file} does not exist.")

#     print(f"Parsing log file: {log_file_path}")

#     # Parse the log file
#     summary = {}
#     run_index = 0
#     current_account = None
#     daily_set_tasks = None
#     desktop_searches_remaining = None
#     read_articles_read = 0

#     for line in log_lines:
#         timestamp_match = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})", line)
#         timestamp = timestamp_match.group(1) if timestamp_match else None

#         if "[INFO] Main Run Started" in line:
#             run_index += 1
#             summary[run_index] = {}
#             current_account = None

#         # Check for account processing
#         account_match = re.search(r"\[POINTS\] Processing account \d+/\d+: (.+)", line)
#         if account_match:
#             current_account = account_match.group(1)
#             summary[run_index][current_account] = {
#                 "daily_set": None,
#                 "desktop_searchs_remaining": None,
#                 "read_articles_remaining": None,
#                 "mobile_searches_remaining": None,
#                 "pts_earned": None,
#                 "account_pts": None,
#                 "ts_start": timestamp,
#                 "ts_end": None,
#             }
#             # Reset variables for new account
#             daily_set_tasks = []
#             desktop_searches_remaining = None
#             read_articles_read = 0
#             mobile_searches_remaining = None
#             in_read_to_earn = False

#         if current_account:
#             # Check for daily set tasks
#             if "[DAILY SET]" in line:
#                 if "Completing search of card" in line:
#                     daily_set_tasks.append(True)
#                 elif "Exiting" in line and daily_set_tasks:
#                     summary[run_index][current_account][
#                         "daily_set"
#                     ] = daily_set_tasks.copy()

#             # Check for desktop searches remaining
#             if "[BING] Starting Desktop Edge Bing searches" in line:
#                 search_type = "desktop"
#             elif "[BING] Starting Mobile Edge Bing searches" in line:
#                 search_type = "mobile"

#             if "[INFO] [BING] Finished Desktop Edge Bing searches" in line:
#                 summary[run_index][current_account]["desktop_searchs_remaining"] = 0
#             elif "[INFO] [BING] Finished Mobile Edge Bing searches" in line:
#                 summary[run_index][current_account]["mobile_searches_remaining"] = 0
#             else:
#                 searches_match = re.search(r"\[BING\] Remaining searches=(\d+)", line)
#                 if searches_match:
#                     searches_remaining = int(searches_match.group(1))
#                     if search_type == "desktop":
#                         summary[run_index][current_account][
#                             "desktop_searchs_remaining"
#                         ] = searches_remaining
#                     elif search_type == "mobile":
#                         summary[run_index][current_account][
#                             "mobile_searches_remaining"
#                         ] = searches_remaining

#             if "[READ TO EARN] Completed the Read to Earn successfully !" in line:
#                 summary[run_index][current_account]["read_articles_remaining"] = 0
#                 in_read_to_earn = False
#             else:
#                 # Check for Read to Earn articles
#                 if "[READ TO EARN]" in line:
#                     in_read_to_earn = True
#                     if "Read Article" in line:
#                         read_articles_read += 1
#                     elif (
#                         "Completed the Read to Earn successfully" in line
#                         or "Exiting" in line
#                     ):
#                         summary[run_index][current_account][
#                             "read_articles_remaining"
#                         ] = (10 - read_articles_read)
#                         in_read_to_earn = False

#             # Check for points earned this run
#             points_match = re.search(
#                 r"\[INFO\] \[POINTS\] You have earned (\d+\.\d+) points this run !",
#                 line,
#             )
#             if points_match:
#                 points_earned = float(points_match.group(1))
#                 summary[run_index][current_account]["pts_earned"] = points_earned

#             # Check for account current points
#             account_points_match = re.search(
#                 r"\[INFO\] \[POINTS\] You are now at ([\d,]+\.\d+) points !", line
#             )
#             if account_points_match:
#                 account_points_str = account_points_match.group(1).replace(",", "")
#                 account_points = float(account_points_str)
#                 summary[run_index][current_account]["account_pts"] = account_points

#             # Update end timestamp for the current account
#             summary[run_index][current_account]["ts_end"] = timestamp

#     return summary


# def view_log_summary(
#     log_file_path=None,
#     summary=None,
#     agg_runs=False,
# ):
#     summary = summary if summary else parse_log(log_file_path)
#     view_data = None
#     rows = []
#     for key, value in summary.items():
#         for email, details in value.items():
#             row = {"log_entry": key, "email": email}
#             row.update(details)
#             rows.append(row)
#     df = pd.DataFrame(rows)
#     df["ts_start"] = df["ts_start"].apply(pd.Timestamp)
#     df["ts_end"] = df["ts_end"].apply(pd.Timestamp)
#     if agg_runs:
#         completion_dict = check_completion(summary=summary)
#         today = datetime.today().date()
#         # Group by account(email) and calculate min ts_start, max ts_end, and the bool column
#         result_df = (
#             df.groupby("email")
#             .agg(min_ts_start=("ts_start", "min"), max_ts_end=("ts_end", "max"))
#             .reset_index()
#         )

#         result_df["run_today"] = result_df["min_ts_start"].dt.date == today
#         # Extract overall completion flag
#         result_df["overall_completion"] = result_df["email"].map(
#             lambda x: completion_dict.get(x, (False, {}))[0]
#         )

#         # Extract task completion flags
#         task_completion_flags = result_df["email"].map(
#             lambda x: completion_dict.get(x, (False, {}))[1]
#         )
#         task_completion_df = pd.DataFrame(
#             task_completion_flags.tolist(), index=result_df.index
#         )

#         # Combine the task completion flags with the result_df
#         result_df = pd.concat([result_df, task_completion_df], axis=1)
#         return result_df
#     else:
#         return df
#         # view_data = tabulate(df, headers="keys", tablefmt="grid")


# def check_completion(
#     summary,
#     ret_type="dict",
# ) -> dict | pd.DataFrame:  # {account: {run_index: {task: bool}}}
#     """Check cumulative completion status and count the number of runs per account."""
#     cumulative_status = {}

#     for run_index, run_data in sorted(summary.items()):
#         for account, account_data in run_data.items():
#             if account not in cumulative_status:
#                 cumulative_status[account] = {}
#                 cumulative_task_completion = {
#                     "ts_start": account_data["ts_start"],
#                     "ts_end": account_data["ts_end"],
#                     "overall_completion": False,
#                     "task.daily_set": False,
#                     "task.desktop_searches": False,
#                     "task.mobile_searches": False,
#                     "task.read_articles": False,
#                 }
#             else:
#                 # Get the latest task completion status
#                 last_run = max(cumulative_status[account].keys())
#                 cumulative_task_completion = cumulative_status[account][last_run].copy()

#             # Update task completion based on current run
#             if (
#                 account_data.get("daily_set")
#                 and account_data["daily_set"].count(True) >= 3
#             ):
#                 cumulative_task_completion["task.daily_set"] = True
#             if account_data.get("desktop_searchs_remaining") == 0:
#                 cumulative_task_completion["task.desktop_searches"] = True
#             if account_data.get("mobile_searches_remaining") == 0:
#                 cumulative_task_completion["task.mobile_searches"] = True
#             if account_data.get("read_articles_remaining") == 0:
#                 cumulative_task_completion["task.read_articles"] = True
#             # print(f"{run_index}, {cumulative_task_completion}")
#             overall_completion = all(
#                 [
#                     v
#                     for k, v in cumulative_task_completion.items()
#                     if k.startswith("task.")
#                 ]
#             )

#             cumulative_status[account][run_index] = {
#                 "ts_start": account_data["ts_start"],
#                 "ts_end": account_data["ts_end"],
#                 "overall_completion": overall_completion,
#                 "task.daily_set": cumulative_task_completion["task.daily_set"],
#                 "task.desktop_searches": cumulative_task_completion[
#                     "task.desktop_searches"
#                 ],
#                 "task.mobile_searches": cumulative_task_completion[
#                     "task.mobile_searches"
#                 ],
#                 "task.read_articles": cumulative_task_completion["task.read_articles"],
#             }
#     if ret_type == "dict":
#         return cumulative_status
#     elif ret_type == "df":
#         return pd.DataFrame(
#             [
#                 {"account": account, "run_index": run_index, **details}
#                 for account, runs in cumulative_status.items()
#                 for run_index, details in runs.items()
#             ]
#         )
#     else:
#         raise ValueError(f"Invalid return type: {ret_type}")
