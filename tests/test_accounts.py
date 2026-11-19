import pytest

from app.accounts import DEFAULT_ID, AccountError, AccountRegistry, account_id_for_sub, owner_of
from app.api import TokenManager

from .helpers import make_config, make_jwt


def stage_login(registry, sub):
    """A staged login as the server leaves it after Tesla accepted it."""
    staged = registry.staging()
    tm = TokenManager(registry.config, staged)
    tm._persist_token(staged.refresh_token_path, make_jwt({"sub": sub, "iat": 1}))
    tm._persist_token(staged.access_token_path, make_jwt({"sub": sub, "exp": 9999999999}))
    return staged


def test_without_registry_there_is_only_the_default_account(tmp_path):
    config = make_config(tmp_path, env_refresh_token="env-token")
    accounts = AccountRegistry(config).accounts()
    assert [a.id for a in accounts] == [DEFAULT_ID]
    assert accounts[0].refresh_token_path == config.refresh_token_path
    assert accounts[0].env_refresh_token == "env-token"


def test_adopt_new_account_moves_tokens_and_registers_name(tmp_path):
    registry = AccountRegistry(make_config(tmp_path))
    staged = stage_login(registry, "user-2")

    account = registry.adopt(staged, None, "  Ana  ", "user-2")

    assert account.id == account_id_for_sub("user-2")
    assert account.refresh_token_path == tmp_path / "accounts" / account.id / "refresh_token.txt"
    assert account.refresh_token_path.exists() and account.access_token_path.exists()
    assert not staged.refresh_token_path.parent.exists()
    assert [(a.id, a.name) for a in registry.accounts()] == [(DEFAULT_ID, ""), (account.id, "Ana")]
    assert TokenManager(registry.config, registry.get(account.id)).user_id() == "user-2"


def test_adopt_without_target_or_name_is_the_default_account(tmp_path):
    config = make_config(tmp_path)
    registry = AccountRegistry(config)

    account = registry.adopt(stage_login(registry, "user-1"), None, None, "user-1")

    assert account.id == DEFAULT_ID
    assert TokenManager(config).user_id() == "user-1"
    assert not registry.registry_path.exists()


def test_reconnecting_an_account_with_another_tesla_login_is_refused(tmp_path):
    registry = AccountRegistry(make_config(tmp_path))
    ana = registry.adopt(stage_login(registry, "user-2"), None, "Ana", "user-2")

    with pytest.raises(AccountError):
        registry.adopt(stage_login(registry, "user-3"), ana.id, None, "user-3")
    assert TokenManager(registry.config, registry.get(ana.id)).user_id() == "user-2"


def test_rename_and_remove(tmp_path):
    registry = AccountRegistry(make_config(tmp_path))
    ana = registry.adopt(stage_login(registry, "user-2"), None, "Ana", "user-2")

    registry.rename(DEFAULT_ID, "Pedro")
    registry.rename(ana.id, "Ana M.")
    assert [a.name for a in registry.accounts()] == ["Pedro", "Ana M."]

    registry.remove(ana.id)
    assert [a.id for a in registry.accounts()] == [DEFAULT_ID]
    assert not (tmp_path / "accounts" / ana.id).exists()

    with pytest.raises(AccountError):
        registry.remove(DEFAULT_ID)
    with pytest.raises(AccountError):
        registry.remove("nope")
    with pytest.raises(AccountError):
        registry.rename("nope", "x")


def test_removed_account_comes_back_with_the_same_id(tmp_path):
    registry = AccountRegistry(make_config(tmp_path))
    first = registry.adopt(stage_login(registry, "user-2"), None, "Ana", "user-2")
    registry.remove(first.id)
    again = registry.adopt(stage_login(registry, "user-2"), None, "Ana", "user-2")
    assert again.id == first.id


def test_owner_of_defaults_pre_multi_account_sidecars():
    assert owner_of({}) == DEFAULT_ID
    assert owner_of({"account": "a123"}) == "a123"


def test_user_id_reads_env_token_and_is_empty_without_tokens(tmp_path):
    assert TokenManager(make_config(tmp_path)).user_id() == ""
    config = make_config(tmp_path, env_refresh_token=make_jwt({"sub": "user-1"}))
    assert TokenManager(config).user_id() == "user-1"
