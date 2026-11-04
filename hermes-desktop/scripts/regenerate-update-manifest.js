#!/usr/bin/env node

import { createRequire } from "node:module";
import { stat, mkdir, writeFile } from "node:fs/promises";
import { basename, dirname, extname, join, resolve } from "node:path";

const require = createRequire(import.meta.url);
const { buildBlockMap } = require("app-builder-lib/out/targets/blockmap/blockmap.js");

function extractVersionFromFilename(filename) {
  const match = filename.match(/Setup-([0-9]+\.[0-9]+\.[0-9]+)/);
  if (match) return match[1];
  throw new Error(`无法从文件名提取版本号: ${filename}。请使用 --version 参数指定。`);
}

function parseArguments(rawArgs) {
  const options = { exePath: null, version: null, outputDir: null };
  for (let index = 0; index < rawArgs.length; index += 1) {
    const arg = rawArgs[index];
    if (arg === "--help" || arg === "-h") {
      options.help = true;
      return options;
    }
    if (arg === "--version" || arg === "--output") {
      const value = rawArgs[index + 1];
      if (!value || value.startsWith("--")) {
        throw new Error(`${arg} 需要一个参数`);
      }
      if (arg === "--version") options.version = value;
      else options.outputDir = value;
      index += 1;
      continue;
    }
    if (arg.startsWith("-")) throw new Error(`未知参数: ${arg}`);
    if (options.exePath !== null) throw new Error("只能指定一个 exe 文件路径");
    options.exePath = arg;
  }
  if (options.exePath === null) throw new Error("请指定 exe 文件路径");
  return options;
}

function printHelp() {
  console.log(`用法: node scripts/regenerate-update-manifest.js <exe-path> [选项]

选项:
  --version <version>   指定版本号（默认从文件名提取）
  --output <dir>        输出目录（默认与 exe 同目录）
  --help, -h            显示帮助

示例:
  node scripts/regenerate-update-manifest.js dist/Hermes-Work-Setup-1.0.22.exe
  node scripts/regenerate-update-manifest.js dist/Hermes-Work-Setup-1.0.22.exe --output release
`);
}

async function assertExecutableFile(filePath) {
  const fileStat = await stat(filePath);
  if (!fileStat.isFile()) throw new Error(`不是普通文件: ${filePath}`);
  if (extname(filePath).toLowerCase() !== ".exe") throw new Error(`仅支持 Windows exe: ${filePath}`);
}

async function writeLatestManifest(outputDir, exePath, version, fileInfo, releaseDate) {
  const exeName = basename(exePath);
  const content = `version: ${version}
files:
  - url: ${exeName}
    sha512: ${fileInfo.sha512}
    size: ${fileInfo.size}
path: ${exeName}
sha512: ${fileInfo.sha512}
releaseDate: '${releaseDate}'
`;
  const manifestPath = join(outputDir, "latest.yml");
  await writeFile(manifestPath, content, "utf8");
  return manifestPath;
}

async function main() {
  const options = parseArguments(process.argv.slice(2));
  if (options.help) {
    printHelp();
    return;
  }

  const exePath = resolve(options.exePath);
  await assertExecutableFile(exePath);
  const outputDir = resolve(options.outputDir ?? dirname(exePath));
  const version = options.version ?? extractVersionFromFilename(basename(exePath));
  if (!/^\d+\.\d+\.\d+$/.test(version)) throw new Error(`版本号必须是 x.y.z: ${version}`);

  await mkdir(outputDir, { recursive: true });
  const blockmapPath = join(outputDir, `${basename(exePath)}.blockmap`);
  console.log(`正在处理: ${exePath}`);
  console.log(`版本号: ${version}`);
  console.log("生成 electron-builder 兼容 blockmap...");
  const fileInfo = await buildBlockMap(exePath, "gzip", blockmapPath);
  console.log(`sha512: ${fileInfo.sha512}`);
  console.log(`文件大小: ${fileInfo.size} bytes`);

  console.log("生成 latest.yml...");
  const manifestPath = await writeLatestManifest(
    outputDir,
    exePath,
    version,
    fileInfo,
    new Date().toISOString(),
  );

  console.log("\n完成! 请将以下文件一起上传到更新源:");
  console.log(`  - ${exePath}`);
  console.log(`  - ${blockmapPath}`);
  console.log(`  - ${manifestPath}`);
}

main().catch((error) => {
  console.error(`错误: ${error instanceof Error ? error.message : String(error)}`);
  process.exitCode = 1;
});
