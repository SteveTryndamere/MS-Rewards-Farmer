# type: ignore
import argparse
import logging
import os
import sys

import pandas as pd
from tabulate import tabulate

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from src.utils import check_completion, parse_log, view_log_summary


def main(log_file_path=None, view="table") -> dict:
    summary = parse_log(log_file_path)
    _ = view_log_summary(summary=summary, view=view, return_view_data=False)
    print(check_completion(summary))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test the log file parser.")
    parser.add_argument("-f", "--file", type=str, help="Path to the log file")
    parser.add_argument(
        "-v", "--view", type=str, default="table", help="View format: 'dict' or 'table'"
    )
    args = parser.parse_args()
    main(log_file_path=args.file, view=args.view)
