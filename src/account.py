from dataclasses import dataclass


@dataclass
class Account:
    username: str
    password: str
    totp: str | None = None
    proxy: str | None = None
    chrome_profile_path: str | None = None
