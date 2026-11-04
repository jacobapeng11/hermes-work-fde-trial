import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, parse } from "node:path";
import { findExternalCommandPath } from "../src/main/command-runner.ts";

// The external-path scan is a reminder layer with a prefer-false-negative
// policy (see the design stance in command-runner.ts). This matrix pins the
// command shapes an agent actually issues; any scanner change that breaks a
// row here would resurface as confusing approval cards in real tasks.

async function makeWorkspace() {
  return mkdtemp(join(tmpdir(), "cowork-command-runner-"));
}

test("matrix: legitimate agent commands never raise an approval card", async () => {
  const workspace = await makeWorkspace();
  const benign = [
    // workspace-relative script execution (the standard deliverable flow)
    'python ".work_ppt/make_snowflake_ppt.py"',
    "python scripts/build.py --out out/report.xlsx",
    'python ".work_ppt/verify.py"',
    "python 报表脚本.py",
    // probes and inline interpreter code
    'python -c "import sys; print(sys.version)"',
    'python -c "print(10 /2)"',
    `python -c "import os;c=[r'C:\\Program Files\\LibreOffice\\program\\soffice.exe',r'C:\\Program Files (x86)\\LibreOffice\\program\\soffice.exe'];print([p for p in c if os.path.exists(p)] or 'NO_SOFFICE')"`,
    `python -c "import glob,os,time; [glob.glob(os.path.expanduser('~')+'/Pictures/*')]"`,
    'node -e "console.log(process.cwd())"',
    // cmd switches and tool flags must not look like posix paths
    `cd /d "${workspace}" && python build.py`,
    "robocopy src dst /XO /E /NFL",
    "git log --oneline -5",
    "dir",
    "echo hello world",
    // bare posix tokens have no meaning under cmd.exe
    "cat /etc/hosts",
    'bash -c "cat /etc/hosts"',
  ];
  for (const command of benign) {
    assert.equal(await findExternalCommandPath(command, workspace), undefined, command);
  }
});

test("matrix: explicit outside-workspace references still raise an approval card", async () => {
  const workspace = await makeWorkspace();
  const root = parse(process.cwd()).root;
  const outsideTxt = join(root, "outside", "secret.txt");
  const soffice = join(root, "Program Files", "LibreOffice", "program", "soffice.exe");
  // quoted, spaces kept whole (not truncated to `C:\Program`)
  assert.equal(await findExternalCommandPath(`type "${outsideTxt}"`, workspace), outsideTxt);
  assert.equal(await findExternalCommandPath(`run "${soffice}" --headless`, workspace), soffice);
  // unquoted absolute reference
  assert.equal(await findExternalCommandPath(`python ${join(root, "outside", "tool.py")}`, workspace), join(root, "outside", "tool.py"));
  // parent traversal escapes the workspace
  assert.ok(await findExternalCommandPath("cd ..\\..", workspace) !== undefined);
  // bare drive root
  assert.ok(await findExternalCommandPath("del D:\\*.tmp", workspace) !== undefined);
});

test("kernel-locked system files classify by geometry instead of failing silently", async () => {
  const workspace = await makeWorkspace();
  // Regression: DumpStack.log is held by the Windows kernel — realpath throws
  // EPERM — and the classifier used to propagate that error, so the command
  // never executed AND never showed an approval card. It must surface as an
  // external-path approval instead.
  const locked = join(parse(process.cwd()).root, "DumpStack.log");
  assert.equal(await findExternalCommandPath(`powershell -Command "Remove-Item -LiteralPath '${locked}' -Force"`, workspace), locked);
});

test("matrix: environment-variable path spellings normalize before scanning", async () => {
  const workspace = await makeWorkspace();
  const root = parse(process.cwd()).root;
  // Regression: the codex-config task read `%USERPROFILE%\.claude\credentials.json`
  // without any approval card — unexpanded variable spellings bypassed the
  // scan entirely. They now expand through the environment first; expanded
  // values that still land inside the workspace never raise a card.
  const outsideCreds = join(root, "Users", "probe", ".claude", "credentials.json");
  process.env.COWORK_PROBE_OUTSIDE = outsideCreds;
  process.env.COWORK_PROBE_INSIDE = workspace;
  try {
    const detected = [
      'type "%COWORK_PROBE_OUTSIDE%"',
      'type "%cowork_probe_outside%"',
      'powershell -NoProfile -Command "Get-Content $env:COWORK_PROBE_OUTSIDE"',
      'powershell -NoProfile -Command "Get-Content ${env:COWORK_PROBE_OUTSIDE}"',
      // Real-USERPROFILE spelling only exists on a Windows host; the probe
      // variables above keep this matrix portable to macOS runners.
      ...(process.platform === "win32"
        ? ['type "%USERPROFILE%\\.claude\\credentials.json"']
        : []),
    ];
    for (const command of detected) {
      assert.ok(await findExternalCommandPath(command, workspace) !== undefined, command);
    }
    const benign = [
      'type "%COWORK_PROBE_INSIDE%\\notes.txt"',
      "for %%f in (*.txt) do @echo %%f",
      "echo %COWORK_UNDEFINED_NAME% & ver",
      "echo %USERNAME%",
    ];
    for (const command of benign) {
      assert.equal(await findExternalCommandPath(command, workspace), undefined, command);
    }
  } finally {
    delete process.env.COWORK_PROBE_OUTSIDE;
    delete process.env.COWORK_PROBE_INSIDE;
  }
});

test("matrix: macOS shell commands (posix platform mode)", async () => {
  const workspace = await makeWorkspace();
  const darwin = "darwin";
  const benign = [
    // workspace-relative script execution — the inner slash must not be
    // sliced into a `/...` posix root (the original mac false positive)
    'python3 ".work_ppt/make_snowflake_ppt.py"',
    "python3 scripts/build.py --out out/report.xlsx",
    // interpreter code bodies are never scanned on any platform
    `python3 -c "import glob,os; glob.glob(os.path.expanduser('~')+'/Pictures/*')"`,
    'node -e "console.log(process.cwd())"',
    "git log --oneline -5",
    "ls -la",
    // `$HOME...` only matches whole-name: `$HOMEWORK` is a different variable.
    "echo $HOMEWORK",
    // absolute path INSIDE the workspace stays unapproved
    `cd ${workspace.replace(/\\/g, "/")} && python3 build.py`,
  ];
  for (const command of benign) {
    assert.equal(await findExternalCommandPath(command, workspace, darwin), undefined, command);
  }
  const detected = [
    "cat /etc/hosts",
    "python3 /Users/agent/outside/tool.py",
    "open ~/Documents/report.pdf",
    "cp /tmp/secret.txt .",
    // NOTE: `$HOME/...` (and braced `${HOME}/...`) expansion cannot be
    // asserted here — the Windows host expands homedir() to a drive-letter
    // form the posix scanner rightly ignores; real darwin hosts cover both
    // spellings end-to-end.
  ];
  for (const command of detected) {
    assert.ok(await findExternalCommandPath(command, workspace, darwin) !== undefined, command);
  }
});
