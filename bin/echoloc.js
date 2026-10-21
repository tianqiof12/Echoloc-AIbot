#!/usr/bin/env node
/**
 * echoloc —— 安装入口与环境自检。
 *
 * 这个 npm 包不含机器人本体。本体是 Python + Docker（LLBot + MaiBot + memwatch），
 * 本包只负责：告诉你该去哪下载、你的机器缺什么。
 */
"use strict";

const { execFileSync } = require("node:child_process");

const REPO = "https://github.com/tianqiof12/Echoloc-QQbot";
const RELEASES = REPO + "/releases/latest";
const DOCS = REPO + "/blob/main/docs/usage.md";
const DESKTOP_DOC = REPO + "/blob/main/docs/release.md";

const C = {
  reset: "\x1b[0m",
  dim: "\x1b[2m",
  bold: "\x1b[1m",
  cyan: "\x1b[36m",
  green: "\x1b[32m",
  yellow: "\x1b[33m",
  red: "\x1b[31m",
  grey: "\x1b[90m",
};
const on = process.stdout.isTTY;
const paint = (s, c) => (on ? c + s + C.reset : s);
const bold = (s) => paint(s, C.bold);

function line(s = "") {
  process.stdout.write(s + "\n");
}

function banner() {
  line();
  line(paint("  ┌─────────────────────────────────────────────┐", C.cyan));
  line(paint("  │", C.cyan) + "   回声 · ECHOLOC                          " + paint("│", C.cyan));
  line(paint("  │", C.cyan) + paint("   一个会遗忘的 QQ 群聊记忆体                ", C.dim) + paint("│", C.cyan));
  line(paint("  └─────────────────────────────────────────────┘", C.cyan));
  line();
}

function usage() {
  banner();
  line(bold("  用法"));
  line("    echoloc            看看这是什么、从哪开始");
  line("    echoloc doctor     检查本机缺什么（docker / python / git）");
  line("    echoloc links      所有相关地址");
  line("    echoloc help       就是这一页");
  line();
  line(bold("  重要"));
  line("    本包不是机器人本体。本体是 Python + Docker，装在 Linux 服务器上；");
  line("    桌面端是 Windows 上的遥控面板，通过 SSH 隧道连服务器的两个 WebUI。");
  line("    " + paint("npm install 跑不起来它。", C.yellow));
  line();
  line("  开始装：" + paint(RELEASES, C.cyan));
  line();
}

function intro() {
  banner();
  line(bold("  它是什么"));
  line("    大多数 QQ 机器人是无状态转发器：收到消息、调模型、发回去，每轮都是陌生人。");
  line("    这个项目的重心在长期状态——事件、情绪、话题、人物印象都带激活度并随时间衰减，");
  line("    好感度、心情、精力是持久值。所以她会记得、会累、也会真的忘掉你。");
  line();
  line(bold("  三个包"));
  line("    " + paint("server (Linux)", C.green) + "   LLBot + MaiBot + memwatch + 面板，Docker 三件套");
  line("    " + paint("desktop (Windows)", C.green) + " pywebview 遥控面板，SSH 隧道连服务器的 WebUI");
  line("    " + paint("android", C.green) + "           WebView 壳，或者直接用手机浏览器开面板端口");
  line();
  line(bold("  三步开始"));
  line("    1. 下载 " + paint(RELEASES, C.cyan));
  line("    2. 解压，按 " + paint(DOCS, C.cyan));
  line("    3. 把所有 CHANGE_ME_* 换成你自己的值（Key、QQ 号、群号、密码）");
  line();
  line(bold("  最低要求"));
  line("    Linux + Docker Compose v2，一个专门当机器人的 QQ 号（别用主号），DeepSeek API Key。");
  line("    1 核 2G 能跑；内存紧就先把桌面端砍掉，那只是看板。");
  line();
  line(paint("  下一句：echoloc doctor 看看你机器缺什么。", C.dim));
  line();
}

function which(bin, args) {
  try {
    const out = execFileSync(bin, args, {
      stdio: ["ignore", "pipe", "ignore"],
      timeout: 8000,
      encoding: "utf8",
    }).trim();
    return out.split("\n")[0].slice(0, 70);
  } catch {
    return null;
  }
}

function doctor() {
  banner();
  line(bold("  环境自检"));
  line();

  const checks = [
    {
      name: "Node.js",
      probe: () => which("node", ["-v"]),
      need: ">= 18",
      required: true,
      hint: "装 Node 18+ 才能跑这个 CLI（但不影响机器人本体）",
    },
    {
      name: "Docker",
      probe: () => which("docker", ["--version"]),
      need: "任意近期版本",
      required: true,
      hint: "服务器端必需。Windows 上要 Docker Desktop 才能跑本地全套",
    },
    {
      name: "Docker Compose",
      probe: () => which("docker", ["compose", "version"]),
      need: "v2",
      required: true,
      hint: "compose.yaml 用的是 v2 语法；只有 docker-compose v1 会跑不起来",
    },
    {
      name: "Python",
      probe: () => which("python3", ["--version"]) || which("python", ["--version"]),
      need: ">= 3.10",
      required: false,
      hint: "只有桌面面板和 webcli 需要；服务器端跑在容器里",
    },
    {
      name: "Git",
      probe: () => which("git", ["--version"]),
      need: "任意",
      required: false,
      hint: "想自己改代码才需要",
    },
    {
      name: "SSH",
      probe: () => which("ssh", ["-V"]),
      need: "任意",
      required: false,
      hint: "桌面端靠它建隧道；没有就只能在服务器上直接用面板",
    },
  ];

  let missingRequired = 0;
  for (const c of checks) {
    const got = c.probe();
    if (got) {
      line("  " + paint("✓", C.green) + " " + c.name.padEnd(16) + paint(got || "ok", C.dim));
    } else {
      const tag = c.required ? paint("✗", C.red) : paint("–", C.yellow);
      line("  " + tag + " " + c.name.padEnd(16) + paint("未检测到", C.dim));
      line("    " + paint("需要 " + c.need + "：" + c.hint, C.grey));
      if (c.required) missingRequired++;
    }
  }

  line();
  if (missingRequired === 0) {
    line(paint("  必需项齐了。下一步：", C.green) + paint(RELEASES, C.cyan));
  } else {
    line(paint("  缺 " + missingRequired + " 项必需的。补齐后再来看。", C.yellow));
    line(paint("  注意：这套东西的主战场是 Linux 服务器，Windows 上跑不全没关系。", C.grey));
  }
  line();
}

function links() {
  banner();
  const rows = [
    ["仓库", REPO],
    ["下载（Releases）", RELEASES],
    ["部署步骤", DOCS],
    ["桌面端说明", DESKTOP_DOC],
    ["问题反馈", REPO + "/issues"],
    ["第三方组件声明", REPO + "/blob/main/THIRD-PARTY-NOTICES.md"],
  ];
  const width = Math.max(...rows.map((r) => r[0].length));
  for (const [k, v] of rows) {
    line("  " + k.padEnd(width) + "  " + paint(v, C.cyan));
  }
  line();
}

function main() {
  const cmd = (process.argv[2] || "").toLowerCase();
  switch (cmd) {
    case "":
    case "intro":
    case "start":
      intro();
      break;
    case "doctor":
    case "check":
      doctor();
      break;
    case "links":
    case "link":
      links();
      break;
    case "help":
    case "-h":
    case "--help":
      usage();
      break;
    case "-v":
    case "--version":
    case "version": {
      const pkg = require("../package.json");
      line(pkg.version);
      break;
    }
    default:
      line();
      line(paint("  不认识的命令：" + cmd, C.yellow));
      line();
      usage();
      process.exitCode = 1;
  }
}

main();
