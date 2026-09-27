"""Isolated executor：不可信生成代码的 Docker Linux 隔离执行适配器。

可靠性方案 Slice 3「execution isolation」：可信的原生工具走 scoped policy；不可信的
生成代码（模型现写现跑的脚本）必须在隔离环境执行，绝不直接落在宿主上。核心安全
不变量：

- 隔离不可用（无 Docker / 无法起容器）时**拒绝执行**（抛 IsolationUnavailableError），
  绝不静默回落到宿主解释器——「跑不了隔离」不等于「那就裸跑」。
- 受限挂载：只把该任务的独立工作目录挂进容器（rw），其余宿主路径默认不可见；敏感
  路径（凭证/密钥/用户目录）显式列为 denied，绝不挂载，工作目录落在其下时直接拒绝。
- 无凭证：不向容器透传宿主环境变量（默认不含 ANTHROPIC_API_KEY 等）。
- 受限网络 / CPU / 内存 / pid / 时间：默认断网、限额、超时强杀。

Docker/WSL 的安装不在本模块职责内；本模块只负责「有则隔离执行，无则拒绝」。
runner 与 availability_checker 均可注入，测试无需真实 Docker。
"""
from __future__ import annotations

import hashlib
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

log = logging.getLogger(__name__)

# 默认禁止挂载的敏感宿主路径片段（凭证/密钥/用户配置）——绝不进容器。
_DEFAULT_DENIED_SUBSTR = (
    ".ssh", ".aws", ".gnupg", ".config", "credentials", ".env",
    "id_rsa", ".kube", "secrets",
)


class IsolationUnavailableError(RuntimeError):
    """隔离环境不可用：调用方必须拒绝该执行模式，不得回落到宿主执行。"""


class IsolationPolicyViolation(RuntimeError):
    """隔离策略被违反（如工作目录落在禁止挂载的敏感路径下）。"""


@dataclass
class IsolationPolicy:
    """隔离执行策略（受限挂载 / 网络 / 资源 / 时间）。"""
    image: str = "python:3.13-slim"
    container_workdir: str = "/work"
    network: bool = False                 # 默认断网
    mem_limit: str = "512m"
    cpus: float = 1.0
    pids_limit: int = 128
    timeout_s: float = 60.0
    read_only_mounts: tuple[str, ...] = ()   # 额外只读挂载（host 路径）
    denied_substr: tuple[str, ...] = field(
        default_factory=lambda: tuple(_DEFAULT_DENIED_SUBSTR))
    user: str = "nobody"                  # 非 root 运行，降权


@dataclass
class IsolatedResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class DockerIsolatedExecutor:
    """在 Docker Linux 容器内执行不可信代码。

    参数：
    - policy: IsolationPolicy（挂载/网络/资源/时间限制）。
    - docker_cmd: docker 可执行名（可注入绝对路径）。
    - runner: 执行 argv 的函数（默认 subprocess.run），签名 (argv, **kw) -> CompletedProcess；
      超时应抛 subprocess.TimeoutExpired。
    - availability_checker: 可注入的可用性探测（默认探测 `docker info`）。
    """

    def __init__(self, policy: IsolationPolicy | None = None, *,
                 docker_cmd: str = "docker",
                 runner: Callable[..., subprocess.CompletedProcess] | None = None,
                 availability_checker: Callable[[], bool] | None = None):
        self.policy = policy or IsolationPolicy()
        self.docker_cmd = docker_cmd
        self._runner = runner or self._default_runner
        self._availability_checker = availability_checker or self._default_available
        self._avail_cache: bool | None = None

    # ---- 可用性 ----

    def _default_runner(self, argv: Sequence[str], **kwargs
                        ) -> subprocess.CompletedProcess:
        return subprocess.run(list(argv), capture_output=True, text=True, **kwargs)

    def _default_available(self) -> bool:
        try:
            cp = subprocess.run([self.docker_cmd, "info"],
                                capture_output=True, text=True, timeout=10,
                                check=False)
        except (OSError, subprocess.SubprocessError):
            return False
        return cp.returncode == 0

    def is_available(self) -> bool:
        """Docker 守护进程是否可用（结果缓存，避免每次执行都探测）。"""
        if self._avail_cache is None:
            self._avail_cache = bool(self._availability_checker())
        return self._avail_cache

    # ---- 挂载安全 ----

    def _assert_mount_allowed(self, host_path: str) -> None:
        """工作目录/只读挂载不得落在敏感路径下（访问拒绝）。"""
        norm = str(Path(host_path).resolve()).replace("\\", "/").lower()
        for bad in self.policy.denied_substr:
            token = bad.lower()
            if f"/{token}" in norm or norm.endswith(token) or f"{token}/" in norm:
                raise IsolationPolicyViolation(
                    f"拒绝挂载敏感路径（命中 {bad!r}）：{host_path}")

    # ---- argv 构造 ----

    def build_argv(self, workdir_host: str, script_name: str) -> list[str]:
        """构造 `docker run` argv，落实受限挂载/断网/资源/时间/降权。

        只挂载该任务的独立工作目录（rw）与显式声明的只读目录；不透传宿主 env。
        """
        self._assert_mount_allowed(workdir_host)
        p = self.policy
        argv = [self.docker_cmd, "run", "--rm", "-i"]
        argv += ["--network", "default" if p.network else "none"]
        argv += ["--memory", p.mem_limit]
        argv += ["--cpus", str(p.cpus)]
        argv += ["--pids-limit", str(p.pids_limit)]
        argv += ["--user", p.user]
        argv += ["--workdir", p.container_workdir]
        # 工作目录 rw 挂载（唯一可写宿主路径）
        argv += ["-v", f"{workdir_host}:{p.container_workdir}"]
        for ro in p.read_only_mounts:
            self._assert_mount_allowed(ro)
            argv += ["-v", f"{ro}:{ro}:ro"]
        argv += [p.image, "python", f"{p.container_workdir}/{script_name}"]
        return argv

    # ---- 执行 ----

    def run_code(self, code: str, *, workdir: str) -> IsolatedResult:
        """在隔离容器内执行一段不可信代码；隔离不可用则拒绝（不回落宿主）。

        代码写入 workdir 下的临时脚本（经挂载进容器），避免 shell 注入；容器内以
        非 root、断网、限额、超时受限运行。
        """
        if not self.is_available():
            raise IsolationUnavailableError(
                "Docker 隔离不可用：拒绝在宿主上执行不可信代码"
                "（安装 Docker/WSL 或改用可信原生工具路径）")
        wd = Path(workdir)
        wd.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(code.encode("utf-8")).hexdigest()[:12]
        script_name = f"_isolated_{digest}.py"
        script_path = wd / script_name
        script_path.write_text(code, encoding="utf-8")
        argv = self.build_argv(str(wd), script_name)
        try:
            cp = self._runner(argv, timeout=self.policy.timeout_s, check=False)
        except subprocess.TimeoutExpired:
            log.warning("isolated exec timed out after %.1fs", self.policy.timeout_s)
            return IsolatedResult(exit_code=-1, stdout="", stderr="",
                                  timed_out=True)
        finally:
            try:
                script_path.unlink()
            except OSError:
                pass
        return IsolatedResult(exit_code=int(cp.returncode),
                              stdout=cp.stdout or "", stderr=cp.stderr or "")
