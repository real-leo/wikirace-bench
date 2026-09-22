# 全链接长时间测试：已排除的环境故障记录

以下运行发生在红链接过滤修复之前，**不纳入正式成绩**。正式结果见
[全链接分组与长时间对照](2026-09-21-grouped-long-comparison.md)。


| 任务 | Jev | SemIf | Laya MLX |
|---|---|---|---|
| DNA → Manipuri pony | 成功，4 点击，56.549 秒 | 成功，5 点击，250.403 秒 | 160 点击，1211.590 秒，红链接 404 |
| Music → 2001 AAA Championships | 成功，9 点击，122.975 秒 | 成功，10 点击，696.915 秒 | 4 点击，72.184 秒，红链接 404 |
| WWII → Bald Mountain Recreation Area | 成功，4 点击，117.363 秒 | 成功，8 点击，563.764 秒 | 发现漏洞后中止 |

原始目录：`runs/grouped-long/formal/`。修复前的运行代码指纹：
`e273d0d52d0e659f7adef863728b2347422bea47350e6ff219dc70158db06c6a`。
Jev 本轮三个响应版本均为 `jev-1.13.0`。

实时网页观测存在一个明确差异：Jev 和 SemIf 的 DNA 初始可用链接均为 456，
Laya 本次为 567。代码指纹相同，但本轮没有冻结网页 DOM，因此不能声称三者
收到了完全相同的候选集合；链接数量变化的具体原因尚未单独定位。

SemIf 三题均完成；600 秒检查点为 2/3 成功，1,800 与 3,600 秒均为 3/3。
Music 在 600 秒时点击 7 次、停在 Diamond League，随后经过 Track and field、AAA Championships，
在 696.915 秒完成。这个例子显示延长时间确实改变了本次成功/失败判定。

SemIf 的实际路径：

- DNA → Genetics → Genetics and archaeogenetics of South Asia → Meitei people → Meitei culture → Manipuri pony。
- Music → Music competition → Music industry → United Kingdom → Birmingham → Alexander Stadium → British Grand Prix (athletics) → Diamond League → Track and field → AAA Championships → 2001 AAA Championships。
- World War II → Western world → Bonnie G. Smith → Rutgers University → New Jersey → List of New Jersey state parks → Lists of state parks by U.S. state → List of Michigan state parks → Bald Mountain Recreation Area。

Music 在 Alexander Stadium 曾完整看到 `Amateur Athletics Association`，却在同组选择了
`British Grand Prix (athletics)`。分组保证了覆盖，并不保证每次选择都走最短路线。

红链接 URL 的候选出现次数（Jev 的 Score 与 Choice 重复出现也计数）：
Jev 三题依次为 14、8、6，SemIf 三题为 12、157、10。
Laya 点击了 `Tuping?action=edit&redlink=1` 与
`Madrid_bid_for_the_1972_Summer_Olympics?action=edit&redlink=1`，收到 HTTP 404。
这些属于环境提供了无效动作，而非“给满 60 分钟仍无法完成”。旧完整日志与进度日志均保留。
