"""测试 isolated_executor：不可信代码的 Docker 隔离执行与访问拒绝。

核心门：隔离不可用时拒绝执行（绝不回落宿主）；受限挂载/断网/降权/限额/超时落到
docker argv；敏感路径（.ssh/secrets 等）拒绝挂载。runner 与 availability_checker
均注入，无需真实 Docker。
"""
import subprocess

import pytest

from agent_core.isolated_executor import (DockerIsolatedExecutor,
                                          IsolatedResult, IsolationPolicy,
                                          IsolationUnavailableError,
                                          IsolationPolicyViolation)


def _fake_runner(capture, returncode=0, stdout="", stderr="",
                 raise_timeout=False):
    def runner(argv, **kwargs):
        capture["argv"] = list(argv)
        capture["kwargs"] = kwargs
        if raise_timeout:
            raise subprocess.TimeoutExpired(cmd=argv,
                                            timeout=kwargs.get("timeout", 1))
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)
    return runner


# ---- 访问拒绝：隔离不可用绝不回落宿主 ----

def test_run_code_rejects_when_docker_unavailable(tmp_path):
    called = {"runner": False}

    def runner(argv, **kw):
        called["runner"] = True
        return subprocess.CompletedProcess(argv, 0, "", "")

    ex = DockerIsolatedExecutor(runner=runner, availability_checker=lambda: False)
    with pytest.raises(IsolationUnavailableError):
        ex.run_code("print(1)", workdir=str(tmp_path / "w"))
    # 关键：拒绝执行时 runner 从未被调用——没有在宿主上裸跑不可信代码
    assert called["runner"] is False


def test_is_available_cached():
    calls = {"n": 0}

    def checker():
        calls["n"] += 1
        return True

    ex = DockerIsolatedExecutor(availability_checker=checker)
    assert ex.is_available() is True
    assert ex.is_available() is True
    assert calls["n"] == 1  # 只探测一次


# ---- argv：受限挂载 / 断网 / 降权 / 限额 ----

def test_build_argv_enforces_restrictions(tmp_path):
    wd = tmp_path / "task-w"
    ex = DockerIsolatedExecutor(availability_checker=lambda: True)
    argv = ex.build_argv(str(wd), "script.py")
    assert argv[0] == "docker" and argv[1] == "run" and "--rm" in argv
    assert argv[argv.index("--network") + 1] == "none"      # 默认断网
    assert argv[argv.index("--memory") + 1] == "512m"
    assert "--cpus" in argv and "--pids-limit" in argv
    assert argv[argv.index("--user") + 1] == "nobody"        # 降权
    assert "-v" in argv and any(str(wd) in a for a in argv)  # 仅挂工作目录
    assert argv[-1].endswith("/work/script.py")              # 容器内脚本路径
    # 不透传宿主环境变量（无 -e / --env-file，凭证不进容器）
    assert "-e" not in argv and "--env-file" not in argv


def test_build_argv_network_enabled(tmp_path):
    ex = DockerIsolatedExecutor(IsolationPolicy(network=True),
                                availability_checker=lambda: True)
    argv = ex.build_argv(str(tmp_path / "task-w"), "s.py")
    assert argv[argv.index("--network") + 1] == "default"


def test_build_argv_readonly_mount(tmp_path):
    wd = tmp_path / "task-w"
    ro = tmp_path / "datasets"
    pol = IsolationPolicy(read_only_mounts=(str(ro),))
    ex = DockerIsolatedExecutor(pol, availability_checker=lambda: True)
    argv = ex.build_argv(str(wd), "s.py")
    assert any(a.endswith(":ro") and str(ro) in a for a in argv)


# ---- 访问拒绝：敏感路径不得挂载 ----

def test_denied_workdir_rejected(tmp_path):
    wd = tmp_path / ".ssh" / "work"
    ex = DockerIsolatedExecutor(availability_checker=lambda: True)
    with pytest.raises(IsolationPolicyViolation):
        ex.build_argv(str(wd), "s.py")


def test_denied_readonly_mount_rejected(tmp_path):
    wd = tmp_path / "task-w"
    ro = tmp_path / "secrets"
    pol = IsolationPolicy(read_only_mounts=(str(ro),))
    ex = DockerIsolatedExecutor(pol, availability_checker=lambda: True)
    with pytest.raises(IsolationPolicyViolation):
        ex.build_argv(str(wd), "s.py")


def test_run_code_denied_workdir_never_runs(tmp_path):
    called = {"runner": False}

    def runner(argv, **kw):
        called["runner"] = True
        return subprocess.CompletedProcess(argv, 0, "", "")

    ex = DockerIsolatedExecutor(runner=runner, availability_checker=lambda: True)
    with pytest.raises(IsolationPolicyViolation):
        ex.run_code("print(1)", workdir=str(tmp_path / ".aws" / "w"))
    assert called["runner"] is False


# ---- 执行路径 ----

def test_run_code_invokes_docker_and_cleans_script(tmp_path):
    wd = tmp_path / "task-w"
    capture = {}
    ex = DockerIsolatedExecutor(
        runner=_fake_runner(capture, returncode=0, stdout="42"),
        availability_checker=lambda: True)
    res = ex.run_code("print(6*7)", workdir=str(wd))
    assert isinstance(res, IsolatedResult)
    assert res.ok and res.exit_code == 0 and res.stdout == "42"
    assert capture["argv"][0] == "docker"
    assert capture["kwargs"]["timeout"] == ex.policy.timeout_s
    # 临时脚本执行后被清理，不残留
    assert not list(wd.glob("_isolated_*.py"))


def test_run_code_timeout_marks_timed_out_and_cleans(tmp_path):
    wd = tmp_path / "task-w"
    ex = DockerIsolatedExecutor(
        IsolationPolicy(timeout_s=1.0),
        runner=_fake_runner({}, raise_timeout=True),
        availability_checker=lambda: True)
    res = ex.run_code("while True: pass", workdir=str(wd))
    assert res.timed_out is True and res.ok is False
    assert not list(wd.glob("_isolated_*.py"))


def test_run_code_nonzero_exit(tmp_path):
    wd = tmp_path / "task-w"
    ex = DockerIsolatedExecutor(
        runner=_fake_runner({}, returncode=1, stderr="boom"),
        availability_checker=lambda: True)
    res = ex.run_code("raise SystemExit(1)", workdir=str(wd))
    assert res.exit_code == 1 and res.ok is False and res.stderr == "boom"
