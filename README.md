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
python3 iphone_duo_checker.py --list-parts         # 看抓到了哪些型号
```

查一次时，有货返回 0，没货返回 1，出错返回 2，方便放进 cron。

说明：

- 型号列表是启动时从 apple.com 的 iPhone Duo 购买页抓的。要是 Apple 改了页面导致抓不到，可以用 `--parts MK1Q4LL/A,...` 手动指定（MK1Q4LL/A = 2TB 星光白 无锁版）。
- 用的是 apple.com 网页“查看门店库存”背后那个没公开的接口，Apple 随时可能改。别查得太频繁，`--watch` 最短 30 秒。
- 据报道，Apple 官网的自提预约要 10/24（周六）才开放，在那之前查到的大概都是没货。

运行测试：`python3 -m unittest test_iphone_duo_checker`
