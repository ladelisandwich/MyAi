import pytest

from homemesh.common.config import HomeMeshConfig, is_loopback_host, load_config, save_config
from homemesh.common.errors import ConfigError
from homemesh.common.paths import default_paths


def test_defaults_are_local_and_safe(home):
    cfg = load_config(home, environ={})
    assert cfg.node.roles == ["coordinator", "worker"]
    assert cfg.server.host == "127.0.0.1"
    assert cfg.server.tls_mode == "off"
    assert cfg.inference.mock_enabled is False
    assert cfg.cloud.enabled is False
    assert cfg.security_problems() == []


def test_env_overrides_nested_and_json(home):
    cfg = load_config(home, environ={"HOMEMESH__SERVER__PORT": "9123", "HOMEMESH__WORKER__ALLOWED_ROOTS": '["/a", "/b"]',
                                     "HOMEMESH_MOCK": "1"})
    assert cfg.server.port == 9123
    assert cfg.worker.allowed_roots == ["/a", "/b"]
    assert cfg.inference.mock_enabled is True


def test_toml_round_trip(home):
    cfg = HomeMeshConfig()
    cfg.node.name = 'pc "quoted" name'
    cfg.worker.allowed_roots = ["/home/u/Docs", "C:\\Users\\u\\Notes"]
    cfg.inference.backends[0].declared_models = {"m:1b": ["completion", "tools"]}
    save_config(cfg, home.config_file)
    again = load_config(home, environ={})
    assert again.model_dump() == cfg.model_dump()


def test_invalid_config_reports_location(home):
    home.config_file.write_text("[server]\nport = 99999\n")
    with pytest.raises(ConfigError) as exc:
        load_config(home, environ={})
    assert "server.port" in exc.value.message


def test_bad_toml_syntax(home):
    home.config_file.write_text("[server\n")
    with pytest.raises(ConfigError) as exc:
        load_config(home, environ={})
    assert "not valid TOML" in exc.value.message


def test_unknown_keys_rejected(home):
    home.config_file.write_text("[server]\nprot = 1\n")
    with pytest.raises(ConfigError):
        load_config(home, environ={})


def test_lan_without_tls_is_refused():
    cfg = HomeMeshConfig()
    cfg.server.host = "0.0.0.0"
    assert cfg.security_problems() and "TLS" in cfg.security_problems()[0]
    cfg.server.tls_mode = "self_signed"
    assert cfg.security_problems() == []
    cfg.server.tls_mode = "off"
    cfg.server.allow_insecure_lan = True
    assert cfg.security_problems() == []


def test_proxy_mode_requires_loopback():
    cfg = HomeMeshConfig()
    cfg.server.tls_mode = "proxy"
    assert cfg.security_problems() == []
    cfg.server.host = "0.0.0.0"
    assert cfg.security_problems()


def test_loopback_detection():
    assert is_loopback_host("127.0.0.1") and is_loopback_host("::1") and is_loopback_host("localhost")
    assert not is_loopback_host("0.0.0.0") and not is_loopback_host("192.168.1.2")


def test_homemesh_home_layout(tmp_path):
    p = default_paths(tmp_path)
    assert p.config_file == tmp_path.resolve() / "homemesh.toml"
    assert p.db_file.parent == tmp_path.resolve() / "data"


def test_unknown_task_type_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        HomeMeshConfig.model_validate({"worker": {"task_types": ["chat", "shell"]}})
