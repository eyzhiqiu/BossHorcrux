"""提供安全重试的本地 Codex 调用封装。"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence as SequenceABC
from typing import Callable, Sequence, Union

Runner = Callable[..., subprocess.CompletedProcess[str]]


class CodexExecutionError(RuntimeError):
    """Codex 调用失败时携带上下文信息的异常。"""

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        attempts: int,
        args: Sequence[str],
        exit_code: int | None = None,
        stdout: str | None = None,
        stderr: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.attempts = attempts
        self.command_args = tuple(args)
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class CodexExecutor:
    """按参数数组调用本地 codex，处理空白输出和重试逻辑。"""

    def __init__(
        self,
        command: Union[str, Sequence[str]] = "codex",
        timeout_seconds: int = 120,
        max_attempts: int = 3,
        runner: Runner = subprocess.run,
    ) -> None:
        if not isinstance(command, str) and not isinstance(command, SequenceABC):
            raise TypeError("command must be str or Sequence[str]")

        if isinstance(command, str):
            self._base_command = [command]
        else:
            self._base_command = [part for part in command]
            if not all(isinstance(part, str) for part in self._base_command):
                raise TypeError("command sequence must contain strings only")

        if not self._base_command:
            raise ValueError("command sequence must not be empty")

        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")

        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.runner = runner

    def generate_markdown(self, prompt: str) -> str:
        attempts = 0

        while attempts < self.max_attempts:
            attempts += 1
            args = self._base_command + [prompt]

            try:
                result = self.runner(
                    args,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                stdout = exc.stdout or ""
                stderr = exc.stderr or ""
                if attempts >= self.max_attempts:
                    raise CodexExecutionError(
                        f"Codex invocation timed out on attempt {attempts}",
                        reason="timeout",
                        attempts=attempts,
                        args=args,
                        stdout=stdout,
                        stderr=stderr,
                    ) from exc
                continue
            except (FileNotFoundError, OSError) as exc:
                raise CodexExecutionError(
                    "Codex command failed to start",
                    reason="spawn_error",
                    attempts=attempts,
                    args=args,
                    stderr=str(exc),
                ) from exc

            stdout = result.stdout or ""
            stderr = result.stderr or ""

            if result.returncode != 0:
                if attempts >= self.max_attempts:
                    raise CodexExecutionError(
                        f"Codex failed after {attempts} attempts (non-zero exit code)",
                        reason="non_zero_exit",
                        attempts=attempts,
                        args=args,
                        exit_code=result.returncode,
                        stdout=stdout,
                        stderr=stderr,
                    )
                continue

            if not stdout.strip():
                if attempts >= self.max_attempts:
                    raise CodexExecutionError(
                        f"Codex failed after {attempts} attempts (empty output)",
                        reason="empty_output",
                        attempts=attempts,
                        args=args,
                        stdout=stdout,
                        stderr=stderr,
                    )
                continue

            return stdout

        raise CodexExecutionError(
            "Codex failed without producing output",
            reason="empty_output",
            attempts=attempts,
            args=self._base_command + [prompt],
        )
