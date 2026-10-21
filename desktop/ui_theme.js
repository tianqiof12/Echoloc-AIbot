/* NapCat / MaiBot 主题统一：浅色/深色由桌面端设置控制 */
(function () {
  var port = String(location.port || "");
  var isNapCat = port === "16099" || location.pathname.indexOf("/webui") === 0;
  var isMaiBot = port === "16097";
  if (!isNapCat && !isMaiBot) return;
  if (document.querySelector('style[data-dsh-unify]')) return;

  var FONT = '-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei","PingFang SC",sans-serif';
  var NAPCAT_CSS = [
    ':root,.light,[data-theme="light"]{',
    '--heroui-primary:221 83% 56%!important;',
    '--heroui-primary-50:214 100% 97%!important;',
    '--heroui-primary-100:214 95% 93%!important;',
    '--heroui-primary-200:213 94% 87%!important;',
    '--heroui-primary-300:213 94% 78%!important;',
    '--heroui-primary-400:213 92% 68%!important;',
    '--heroui-primary-500:221 83% 56%!important;',
    '--heroui-primary-600:221 75% 48%!important;',
    '--heroui-primary-700:221 70% 40%!important;',
    '--heroui-primary-800:221 65% 32%!important;',
    '--heroui-primary-900:221 60% 24%!important;',
    '--heroui-primary-foreground:0 0% 100%!important;',
    '--heroui-focus:221 83% 56%!important;',
    '--heroui-secondary:221 60% 70%!important;',
    '--heroui-background:220 33% 98%!important;',
    '--heroui-foreground:222 25% 15%!important;',
    '--heroui-content1:0 0% 100%!important;',
    '--heroui-content2:220 33% 98%!important;',
    '--heroui-content3:220 20% 94%!important;',
    '--heroui-content4:220 15% 88%!important;',
    '--heroui-default:220 14% 92%!important;',
    '--heroui-default-foreground:222 25% 15%!important;',
    '--heroui-divider:220 15% 88%!important;',
    '}',
    'html,body{background:#f5f7fb!important;color:#222a3a!important;font-family:' + FONT + '!important}',
    '[class*="from-pink-50"],[class*="via-pink"],[class*="to-pink-50"]{',
    '--tw-gradient-from:#f6f8fc!important;--tw-gradient-via:#f2f5fb!important;--tw-gradient-to:#eef2f9!important}',
    '[class*="bg-pink-"]{background-color:rgba(47,111,237,.10)!important}',
    '[class*="text-pink-"]{color:#2f6fed!important}',
    '[class*="bg-primary-200"]{background-color:rgba(47,111,237,.12)!important}',
    '[class*="bg-secondary-200"]{background-color:rgba(47,111,237,.08)!important}',
    '[style*="background-size: cover"]{background-image:none!important;background-color:#f5f7fb!important}',
    'img[alt="background"]{display:none!important}',
    'svg circle[stroke="#EA7D9B"]{stroke:#2f6fed!important}',
    'svg circle[stroke="#D33FF0"]{stroke:#7aa7ff!important}',
    '[class*="bg-danger"]{background-color:#eef4ff!important}',
    '[class*="text-danger"]{color:#2f6fed!important}',
    '[class*="border-danger"]{border-color:#d6e4ff!important}',
    '::-webkit-scrollbar{width:8px;height:8px}',
    '::-webkit-scrollbar-thumb{background:#c7d2e4;border-radius:4px}'
  ].join("");
  var NAPCAT_DARK = [
    'html.dark,html.dark body{background:#0f131a!important;color:#e6ebf5!important}',
    'html.dark{--heroui-background:222 25% 8%!important;--heroui-foreground:210 30% 92%!important;',
    '--heroui-content1:222 22% 12%!important;--heroui-content2:222 22% 15%!important;',
    '--heroui-content3:222 20% 18%!important;--heroui-content4:222 18% 22%!important;',
    '--heroui-default:222 18% 22%!important;--heroui-default-foreground:210 30% 92%!important;',
    '--heroui-divider:222 18% 24%!important}',
    'html.dark [class*="bg-white"]{background-color:#171d28!important}',
    'html.dark [class*="bg-zinc-50"]{background-color:#171d28!important}',
    'html.dark [class*="text-gray-900"],html.dark [class*="text-zinc-900"]{color:#e6ebf5!important}',
    'html.dark [class*="text-gray-500"],html.dark [class*="text-zinc-500"]{color:#9aa7bd!important}',
    'html.dark [class*="from-pink-50"]{--tw-gradient-from:#151b26!important;--tw-gradient-via:#111722!important;--tw-gradient-to:#0f131a!important}',
    'html.dark [style*="background-size: cover"]{background-image:none!important;background-color:#0f131a!important}',
    'html.dark ::-webkit-scrollbar-thumb{background:#2c3a52}'
  ].join("");
  var MAIBOT_CSS = [
    ':root,html.light,[data-theme="light"]{',
    '--color-primary:221 83% 56%!important;',
    '--color-primary-foreground:0 0% 100%!important;',
    '--color-primary-gradient:none!important;',
    '--color-accent:221 70% 60%!important;',
    '--color-accent-foreground:0 0% 100%!important;',
    '--color-background:220 33% 98%!important;',
    '--color-foreground:222 25% 15%!important;',
    '--color-card:0 0% 100%!important;',
    '--color-card-foreground:222 25% 15%!important;',
    '--color-muted:220 20% 94%!important;',
    '--color-muted-foreground:220 12% 45%!important;',
    '--color-border:220 15% 88%!important;',
    '--color-input:220 15% 88%!important;',
    '--color-ring:221 83% 56%!important;',
    '--primary:221 83% 56%!important;',
    '--accent:221 70% 60%!important;',
    '--background:220 33% 98%!important;',
    '--foreground:222 25% 15%!important;',
    '--retro-paper:#f5f7fb!important;',
    '--retro-paper-deep:#ffffff!important;',
    '--retro-paper-soft:#ffffff!important;',
    '--retro-ink:#222a3a!important;',
    '}',
    'html,body,#app{background:#f5f7fb!important;color:#222a3a!important;font-family:' + FONT + '!important}',
    'button,input,select,textarea{font-family:' + FONT + '!important}',
    '::-webkit-scrollbar{width:8px;height:8px}',
    '::-webkit-scrollbar-thumb{background:#c7d2e4;border-radius:4px}'
  ].join("");
  var MAIBOT_DARK = [
    'html.dark,html.dark body,html.dark #app{background:#0f131a!important;color:#e6ebf5!important}',
    'html.dark{--color-background:222 25% 8%!important;--color-card:222 22% 12%!important;',
    '--color-foreground:210 30% 92%!important;--color-card-foreground:210 30% 92%!important;',
    '--color-muted:222 20% 16%!important;--color-muted-foreground:215 15% 65%!important;',
    '--color-border:222 18% 22%!important;--color-input:222 18% 22%!important;',
    '--background:222 25% 8%!important;--foreground:210 30% 92%!important;',
    '--retro-paper:#0f131a!important;--retro-paper-deep:#171d28!important;',
    '--retro-paper-soft:#171d28!important;--retro-ink:#e6ebf5!important}',
    'html.dark [class*="bg-white"]{background-color:#171d28!important}',
    'html.dark [class*="text-black"]{color:#e6ebf5!important}',
    'html.dark ::-webkit-scrollbar-thumb{background:#2c3a52}'
  ].join("");

  var MODE = "light";
  try {
    if (isNapCat) {
      var saved = localStorage.getItem("theme");
      if (saved) MODE = JSON.parse(saved) === "dark" ? "dark" : "light";
    } else {
      MODE = localStorage.getItem("maibot-theme-mode") === "dark" ? "dark" : "light";
    }
  } catch (e) {}

  function hideNapCatThemeToggle() {
    document.querySelectorAll("button").forEach(function (b) {
      var text = (b.textContent || "").replace(/\s+/g, "");
      if (text === "切换主题") {
        b.style.display = "none";
        b.setAttribute("data-dsh-hidden", "1");
      }
    });
  }

  function applyMode(mode, save) {
    MODE = mode === "dark" ? "dark" : "light";
    var root = document.documentElement;
    if (!root) return;
    root.classList.remove("dark", "light");
    root.classList.add(MODE);
    if (isMaiBot) root.dataset.dashboardStyle = "modern";
    if (save !== false) {
      try {
        if (isNapCat) localStorage.setItem("theme", JSON.stringify(MODE));
        else localStorage.setItem("maibot-theme-mode", MODE);
      } catch (e) {}
    }
    if (isNapCat) hideNapCatThemeToggle();
  }

  function boot() {
    var root = document.documentElement;
    if (!root) return;
    var s = document.createElement("style");
    s.setAttribute("data-dsh-unify", "1");
    s.textContent = isNapCat ? (NAPCAT_CSS + NAPCAT_DARK) : (MAIBOT_CSS + MAIBOT_DARK);
    (document.head || root).appendChild(s);
    applyMode(MODE, false);
    if (isNapCat) {
      localStorage.removeItem("background-image");
    }

    var mo = new MutationObserver(function () {
      if (!root.classList.contains(MODE)) {
        root.classList.remove("dark", "light");
        root.classList.add(MODE);
      }
      if (isMaiBot && root.dataset.dashboardStyle !== "modern") {
        root.dataset.dashboardStyle = "modern";
      }
      if (isNapCat) hideNapCatThemeToggle();
    });
    mo.observe(root, { attributes: true, attributeFilter: ["class", "data-dashboard-style"] });

    if (isNapCat) {
      hideNapCatThemeToggle();
      setInterval(hideNapCatThemeToggle, 1200);
      if (document.body) {
        new MutationObserver(hideNapCatThemeToggle).observe(document.body, {
          childList: true,
          subtree: true
        });
      }
    }
  }

  window.addEventListener("message", function (ev) {
    var d = ev.data;
    if (d && d.type === "dsh-theme") applyMode(d.mode, true);
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
