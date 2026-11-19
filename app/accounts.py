"""Several Tesla accounts in one instance.

The *default* account is the single account the app always had: its tokens
live at the configured REFRESH_TOKEN_PATH / ACCESS_TOKEN_PATH (or come from
the env vars / Home Assistant options), so existing installs need no
migration. Extra accounts are added in the dashboard only; each keeps its
tokens in ``<secrets dir>/accounts/<id>/``. ``<secrets dir>/accounts.json``
holds every account's display name.

An extra account's id is derived from the Tesla user id (the ``sub`` claim
of its tokens): signing the same Tesla user in again after removing it gets
the same id back, and with it the invoices recorded under that id.
"""

import hashlib
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

from app import storage
from app.config import Config

DEFAULT_ID = "default"
REFRESH_TOKEN_FILE = "refresh_token.txt"
ACCESS_TOKEN_FILE = "access_token.txt"
NAME_MAX_LENGTH = 40


class AccountError(Exception):
    pass


@dataclass
class Account:
    id: str
    name: str
    refresh_token_path: Path
    access_token_path: Path
    env_refresh_token: str = ""
    env_access_token: str = ""


def account_id_for_sub(sub: str) -> str:
    # Hashed, not the raw Tesla user id: the id ends up in every sidecar and
    # in the dashboard's API responses.
    return "a" + hashlib.sha256(sub.encode()).hexdigest()[:10]


def owner_of(metadata: dict) -> str:
    """The account an invoice sidecar belongs to. Sidecars written before
    multi-account support have no ``account`` field; they can only have come
    from the default account."""
    return str(metadata.get("account") or DEFAULT_ID)


def clean_name(name: str | None) -> str:
    return " ".join((name or "").split())[:NAME_MAX_LENGTH]


class AccountRegistry:
    def __init__(self, config: Config):
        self.config = config
        self.secrets_dir = config.refresh_token_path.parent
        self.registry_path = self.secrets_dir / "accounts.json"
        self.accounts_dir = self.secrets_dir / "accounts"

    def _names(self) -> dict[str, str]:
        data = storage.read_json(self.registry_path)
        return {k: str((v or {}).get("name") or "") for k, v in data.items() if isinstance(v, dict)}

    def _save_names(self, names: dict[str, str]) -> None:
        storage.write_json_atomic(self.registry_path, {k: {"name": v} for k, v in names.items()})

    def _extra(self, account_id: str, name: str) -> Account:
        directory = self.accounts_dir / account_id
        return Account(account_id, name, directory / REFRESH_TOKEN_FILE, directory / ACCESS_TOKEN_FILE)

    def default(self) -> Account:
        return Account(
            DEFAULT_ID,
            self._names().get(DEFAULT_ID, ""),
            self.config.refresh_token_path,
            self.config.access_token_path,
            self.config.env_refresh_token,
            self.config.env_access_token,
        )

    def accounts(self) -> list[Account]:
        """Every account, the default one first, the others in the order
        they were added."""
        names = self._names()
        extras = [self._extra(i, n) for i, n in names.items() if i != DEFAULT_ID]
        return [self.default(), *extras]

    def get(self, account_id: str) -> Account | None:
        return next((a for a in self.accounts() if a.id == account_id), None)

    def staging(self) -> Account:
        """A throwaway account for a login in progress: its tokens are only
        moved to a real account once Tesla accepted them and they were
        checked against the accounts already connected."""
        return self._extra(f".pending-{secrets.token_hex(6)}", "")

    def discard(self, staged: Account) -> None:
        shutil.rmtree(staged.refresh_token_path.parent, ignore_errors=True)

    def adopt(self, staged: Account, target_id: str | None, name: str | None, sub: str) -> Account:
        """Move a staged login's tokens to their account and return it.

        ``target_id`` set: reconnect that account. ``target_id`` None with a
        ``name``: a new account. Neither: the default account (the app's
        behaviour before multi-account support).
        """
        names = self._names()
        if target_id is None and name is not None:
            target = self._extra(account_id_for_sub(sub), clean_name(name))
            names[target.id] = target.name
        elif target_id in (None, DEFAULT_ID):
            target = self.default()
        else:
            existing = self.get(target_id)
            if existing is None:
                raise AccountError("Unknown account")
            if account_id_for_sub(sub) != existing.id:
                raise AccountError(
                    "This is a different Tesla login than the account's — add it as a new account instead"
                )
            target = existing

        for src, dst in (
            (staged.refresh_token_path, target.refresh_token_path),
            (staged.access_token_path, target.access_token_path),
        ):
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                src.replace(dst)
        self.discard(staged)
        if target.id in names:
            self._save_names(names)
        return target

    def rename(self, account_id: str, name: str) -> None:
        names = self._names()
        if account_id != DEFAULT_ID and account_id not in names:
            raise AccountError("Unknown account")
        names[account_id] = clean_name(name)
        self._save_names(names)

    def remove(self, account_id: str) -> None:
        """Forget an extra account and delete its tokens. Its invoices stay."""
        if account_id == DEFAULT_ID:
            raise AccountError("The main account cannot be removed")
        names = self._names()
        if account_id not in names:
            raise AccountError("Unknown account")
        del names[account_id]
        self._save_names(names)
        shutil.rmtree(self.accounts_dir / account_id, ignore_errors=True)
