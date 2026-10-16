# hello-world
a repository
hello world

## iPhone Duo 自提库存查询

`iphone_duo_checker.py` 查邮编（默认 91016）附近 Apple Store 能不能自提 iPhone Duo。所有颜色、所有容量都会查，默认也包括所有运营商版本。只用 Python 3 标准库，不需要装任何东西。

```bash
python3 iphone_duo_checker.py                      # 查一次，只显示有货的门店
python3 iphone_duo_checker.py --all                # 每家店每个型号的状态都显示
python3 iphone_duo_checker.py --watch 60           # 每分钟查一次，有新库存时响铃并弹系统通知
python3 iphone_duo_checker.py --watch 60 --ntfy my-duo-alert   # 同时推送到手机（ntfy App 订阅 my-duo-alert）
python3 iphone_duo_checker.py --carrier unlocked   # 只看无锁版
python3 iphone_duo_checker.py --max-miles 20       # 只看 20 英里以内
python3 iphone_duo_checker.py --delivery           # 顺便查寄送到这个邮编的最早日期
python3 iphone_duo_checker.py --list-parts         # 看抓到了哪些型号
```

查一次时，有货返回 0，没货返回 1，出错或被 Apple 拦截返回 2，方便放进 cron。

### 不用电脑：GitHub Actions 自动查

`.github/workflows/iphone-duo-check.yml` 自动查 91016 附近门店的自提库存和寄送日期，频率跟着 Apple 的状态走：

- **自提还没开放时**（每家店都显示 “Check back for availability on …”）：每 5 分钟左右查一次。这时候显示的是 Apple 关了自提功能，不是按库存算的，查得再勤也没用。
- **自提一开放**：自动切到每分钟查一次。每次运行连续查 20 分钟，结束前自己启动下一轮，所以中间不会断；每 5 分钟的定时任务负责看门，万一接力断了（比如某台 GitHub 机器被 Apple 拦截），它会把检查重新拉起来。
- **寄送日期**：每 5 分钟查一次寄送到 91016 的最早日期。

通知都通过 issue @ 仓库主人，手机上的 GitHub App 或邮箱会收到：

- 自提一有货，就开一个标题为「iPhone Duo 有货可自提（91016 附近）」的 issue。有货的门店或型号变了，在这个 issue 下追加评论；全部没货时自动关闭这个 issue，下次有货再开新的。
- 寄送日期放在一个一直开着的 issue「iPhone Duo 寄送日期（送到 91016）」里，正文显示每个型号现在的日期和见过的最早日期，改正文不会发通知。只有某个型号的日期比之前见过的都早，才会在下面评论并 @ 你；日期往后推或者来回跳都不打扰你。
- 想确认手机能不能收到通知：在 GitHub 上打开 Actions → iPhone Duo pickup check → Run workflow，勾选「发一条测试提醒」。
- 不想查了：Actions → iPhone Duo pickup check → 右上角 `…` → Disable workflow（只取消某一次运行没用，看门的定时任务几分钟后会重新启动）。
- 每次运行的完整结果（每家店每个型号的状态）都在 Actions 的运行日志里。

说明：

- 型号列表是启动时从 apple.com 的 iPhone Duo 购买页抓的。要是 Apple 改了页面导致抓不到，可以用 `--parts MK1Q4LL/A,...` 手动指定（MK1Q4LL/A = 2TB 星光白 无锁版）。
- 查库存用的是 apple.com 网页“查看门店库存”背后的接口 `/shop/retail/pickup-message`，寄送日期用的是 `/shop/delivery-message`（邮编要用 `postalCode` 参数传，传 `location` 会被忽略，改成按 IP 定位）。这个接口没公开，Apple 随时可能改。老接口 `/shop/fulfillment-messages` 现在会返回 HTTP 541，只留作备用。
- HTTP 541、403、429 都是 Apple 拒绝了这次请求，**不代表没货**。脚本会报错，不会把它当成没货；`--watch` 模式下会自动等 5 到 30 分钟再试。别查得太频繁，`--watch` 最短 30 秒。
- Apple 官网写着自提要 10/24（周六）才开放，在那之前每家店都会显示 “Check back for availability on 10.24”。

运行测试：`python3 -m unittest test_iphone_duo_checker`
