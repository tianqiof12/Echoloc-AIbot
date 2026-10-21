# Android 壳

`QQbotPanel.apk` 是一个 WebView 壳，加载服务器上的 `8090` 面板。

> 为了不把带默认地址/密码的二进制带进便携包，这个目录**不包含 APK**；沿用 `E:\QQbot\QQbotPanel.apk`，或自行重新打包。

## 使用

1. 安装 APK。
2. 首次启动默认地址是 `http://服务器IP:8090/`；如果换了服务器，长按页面进入设置，改地址、用户名、密码。
3. 账号密码就是 `server/panel/qqbot-panel.service` 里的 `PANEL_USER` / `PANEL_PASSWORD`。

## 注意

- APK 里带的默认地址/密码是旧机的，**换服务器后必须长按修改**，否则连不上。
- 面板是 HTTP + Basic Auth，公网使用建议在前面加 HTTPS 反向代理。
- APK 只是壳，菜单、页面、功能都来自服务器面板；改面板不需要重新打包 APK。
