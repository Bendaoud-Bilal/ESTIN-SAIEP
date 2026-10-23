"""Single place where the CLI executes commands (mockable, dry-run capable)."""
import os
import shlex
import shutil
import subprocess


class CommandError(RuntimeError):
    def __init__(self, cmd, code, output):
        super().__init__(f'command failed ({code}): {shlex.join(cmd)}\n{output}')
        self.cmd, self.code, self.output = cmd, code, output


class Runner:
    def __init__(self, dry_run=False, echo=None):
        self.dry_run = dry_run
        self.echo = echo  # callable(str) for showing commands, or None
        self.history = []

    def which(self, name):
        return shutil.which(name)

    def run(self, cmd, *, check=True, capture=True, input=None, interactive=False,
            env=None, cwd=None, echo=True):
        """Run cmd (list). Returns stdout (str) when captured.

        env is merged into the current environment; cwd defaults to the caller's
        working directory. echo=False hides noisy polling commands from the UI.
        """
        self.history.append(list(cmd))
        if self.echo and echo:
            self.echo(shlex.join(cmd))
        if self.dry_run:
            return ''
        run_env = None
        if env is not None:
            run_env = os.environ.copy()
            run_env.update(env)
        if interactive:
            code = subprocess.call(cmd, cwd=cwd, env=run_env)
            if check and code:
                raise CommandError(cmd, code, '')
            return ''
        proc = subprocess.run(
            cmd, input=input, text=True, cwd=cwd, env=run_env,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None,
        )
        if check and proc.returncode:
            raise CommandError(cmd, proc.returncode, proc.stdout or '')
        return proc.stdout or ''

    def ok(self, cmd, *, echo=True):
        """True if cmd exits 0 (never raises)."""
        try:
            self.run(cmd, check=True, echo=echo)
            return True
        except (CommandError, FileNotFoundError):
            return False
