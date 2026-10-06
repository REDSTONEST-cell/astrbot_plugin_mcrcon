## astrbot_plugin_mcman

# MC管理器

### 一个基于RCON协议的MC服务器管理器插件

/mcwl 相当于在游戏中执行whitelist {option} {mcname(可选)}

/mcban 相当于在游戏中执行ban {mcname}

/mcpardon 相当于在游戏中执行pardon {mcname}

/mcbanlist 相当于在游戏中执行banlist

# 支持

[帮助文档](https://astrbot.app)

/wantwl {mcname} 玩家自助申请白名单并绑定 QQ（需在配置中开启）

/mcwho {mcname} 反查 MC 账号绑定的 QQ（管理员）

### 绑定数据

QQ 与 MC 账号的绑定记录保存在 `data/plugin_data/astrbot_plugin_mcman/bindings.db`（SQLite）。
旧版的 `apply_whitelist.json` 会在插件启动时自动导入，并重命名为 `apply_whitelist.json.migrated`。

### 退群自动解绑

使用 OneBot v11（aiocqhttp）适配器时，绑定过的 QQ 退群或被踢后会自动解绑，
并（可配置）把对应 MC 账号移出白名单。可通过 `unbind_groups` 限定只监听指定的群。
