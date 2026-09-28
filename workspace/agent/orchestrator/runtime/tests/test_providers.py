"""runtime.providers：ProviderStore 池管理 + ${ENV} 解析 + 密钥遮蔽。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from providers import ProviderStore


def make_store(tmp_path, **env):
    return ProviderStore(tmp_path / "providers.json", env=env or {"T": "tok"})


def test_empty_store_defaults(tmp_path):
    ps = make_store(tmp_path)
    assert ps.names() == [] and ps.default() == ""
    assert ps.list_all() == {"default": "", "providers": {}}
    assert ps.resolve("nope") is None


def test_upsert_and_resolve(tmp_path):
    ps = make_store(tmp_path, T="tok123", U="https://gw.example.com")
    ps.upsert("main", model="sonnet", base_url="${U}/v1", auth_token="sk-${T}")
    assert ps.resolve("main") == {
        "name": "main", "model": "sonnet",
        "base_url": "https://gw.example.com/v1", "auth_token": "sk-tok123",
    }


def test_resolve_plain_values_and_no_endpoint(tmp_path):
    ps = make_store(tmp_path)
    ps.upsert("p2", model="haiku", base_url="", auth_token="plain")
    r = ps.resolve("p2")
    assert r["base_url"] is None and r["auth_token"] == "plain"
    assert r["model"] == "haiku"


def test_resolve_unresolvable_env_returns_none(tmp_path):
    ps = make_store(tmp_path, X="1")
    ps.upsert("broken", model="x", base_url="${NOPE}/v1")
    assert ps.resolve("broken") is None  # 缺需环境变量 → 视同不可用


def test_default_roundtrip_and_keyerror(tmp_path):
    ps = make_store(tmp_path)
    ps.upsert("a", model="s")
    ps.set_default("a")
    assert ps.default() == "a"
    ps.set_default("")  # 清空
    assert ps.default() == ""
    with pytest.raises(KeyError):
        ps.set_default("ghost")


def test_list_masks_auth_token(tmp_path):
    ps = make_store(tmp_path)
    ps.upsert("a", model="s", auth_token="super-secret-key")
    view = ps.list_all()
    tok = view["providers"]["a"]["auth_token"]
    assert tok.startswith("***") and "super-secret" not in tok
    assert tok.endswith("key")


def test_delete_clears_default(tmp_path):
    ps = make_store(tmp_path)
    ps.upsert("a", model="s")
    ps.set_default("a")
    assert ps.delete("a") is True
    assert ps.default() == "" and ps.names() == []
    assert ps.delete("a") is False


def test_persistence_across_reload(tmp_path):
    p = tmp_path / "providers.json"
    ps = ProviderStore(p, env={"T": "t"})
    ps.upsert("main", model="sonnet", base_url="${U}", auth_token="x")
    ps.set_default("main")
    ps2 = ProviderStore(p, env={"T": "t", "U": "https://gw"})
    assert ps2.default() == "main"
    assert ps2.resolve("main")["base_url"] == "https://gw"


def test_corrupt_file_starts_empty(tmp_path):
    p = tmp_path / "providers.json"
    p.write_text("{broken json", encoding="utf-8")
    ps = ProviderStore(p)
    assert ps.names() == [] and ps.default() == ""  # 不崩溃，按空池启动