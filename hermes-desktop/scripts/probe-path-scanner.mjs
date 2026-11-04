import { referencedPaths } from "../src/main/command-runner.ts";

const cases = [
  `chcp 65001 >nul & powershell -NoProfile -Command "Remove-Item 'D:\\DumpStack.log' -Force -ErrorAction Continue; if (Test-Path 'D:\\DumpStack.log') { 'STILL_EXISTS' } else { 'DELETED_OK' }"`,
  `chcp 65001 >nul & powershell -NoProfile -Command "Test-Path 'D:\\DumpStack.log'; (Get-Item 'D:\\DumpStack.log' -Force).Mode"`,
  `chcp 65001 >nul & powershell -NoProfile -Command "'nul_test'; Test-Path 'D:\\output_test.docx'; Test-Path 'D:\\输出.docx'; Test-Path 'D:\\pagefile.sys'"`,
  `chcp 65001 >nul & powershell -NoProfile -Command "Remove-Item -LiteralPath 'D:\\DumpStack.log' -Force; Test-Path 'D:\\DumpStack.log'"`,
];

for (const command of cases) {
  console.log("CMD   :", command.slice(0, 110));
  console.log("TOKENS:", JSON.stringify(referencedPaths(command)));
  console.log();
}
