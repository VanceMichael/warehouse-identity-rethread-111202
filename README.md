# identity-rethread

面向既有事实的客户归属重织：上游客户主数据系统下发合并/拆分决议后，数据仓库
按决议版本为历史合同事实重新生成代理键与集市结果，不覆盖来源键、不改写原始事实，
也不建设候选匹配、人工确认或客户维护服务。

## 数据组织

来源层（只读输入，`warehouse/definition.py`）：

- `identities`：`source_key TEXT PRIMARY KEY, entity TEXT NOT NULL`
- `contracts`：`contract TEXT PRIMARY KEY, source_key TEXT NOT NULL, amount TEXT NOT NULL, signed_on TEXT NOT NULL`
  - 每份事实保留原始来源身份（`系统名:主键`）与发生时间 `signed_on`，重织只追加按代次的代理键。

决议层（上游给定的本地输入）：

- `resolutions` / `resolution_entities` / `resolution_mappings`：决议带唯一编号、递增版本、
  生效区间（半开 `[valid_from, valid_to)`）与来源身份到统一实体的完整映射，支持多身份合并
  与部分身份拆出。决议内容以 SHA-256 摘要固定：同编号同版本同内容重放无影响，同编号同版本
  不同内容阻断。装载前校验同版重复、同一身份区间冲突（端点相接允许）、非法实体引用与版本递增。

重织层（按代次追加，从不原地更新）：

- `dim_entity`：统一实体维度，代理键单调分配；`0` 预留给未知维 `#unknown`。拆分产生的实体
  只能取得新代理键，不会复用其他实体的历史身份。
- `generations` / `generation_decisions`：一次发布是一个可读代次，记录所用决议版本与
  决议/输入指纹。
- `fact_rw`：按代次的事实代理键（原始来源键与发生时间原样保留）。
- `mart_rw`：按代次 × 月份 × 代理键的集市。累计签约额按每份合同全额累加；去重活跃客户数
  按代理键去重——金额守恒不代表人数正确。
- `partition_builds`：每个分区的输入指纹与映射指纹，恢复只复用两者都未变化的分区。
- `published_pointer`：当前发布代次指针；`publication_locks` 保证两个发布者不会同时构建，
  输者收到冲突错误，读者只能看到完整旧代次或完整新代次。

`examples/source.json` 为本地合成输入，`examples/resolution-*.json` 为虚构标识的决议样例
（合并、局部拆分、未知补档、跨期改属），不接客户系统。`schema.json` 描述全部结构。

## 归属规则

- 事实按 `signed_on` 落入覆盖该日期的映射片段；没有片段覆盖时进入未知维，补决议后在新一代次
  回填，旧代次保持未知。
- 某来源身份跨期改属时，只有生效日后的事实改属，整段历史不会无条件挪到新客户。
- 重算范围只包含映射区间与事实分区交叠、且确有受影响身份事实的月份，无关月份完全不扫描；
  未受影响分区从上一代次复制（指纹双校验）。
- 重织期间到达的合同先进入来源事实，预览是不落库的影子结果；新合同只在再次发布的新一代次
  可见，已发布代次永不被覆盖。

## 环境与验证

Python 3.11 以上，仅使用标准库，无第三方安装。`runtime.txt` 记录验证解释器版本。数据库和
对象目录均由入口参数指定。

```bash
python3 -m compileall -q warehouse tests scripts
python3 -m unittest discover -s tests -v
```

## 运行

来源装载与静态一对一查询（原有能力）：

```bash
python3 -m warehouse import --db /tmp/identity-rethread.sqlite3 --input examples/source.json --batch first
python3 -m warehouse inspect --db /tmp/identity-rethread.sqlite3
python3 -m warehouse export --db /tmp/identity-rethread.sqlite3 --objects /tmp/identity-rethread-objects
```

重织流程：先接收决议、预览差异（受影响合同、日期分区、集市指标写入预览文件），维护者确认后
再发布；旧报告可按原代次查询。

```bash
python3 -m warehouse resolution --db /tmp/identity-rethread.sqlite3 --input examples/resolution-1-merge.json
python3 -m warehouse preview   --db /tmp/identity-rethread.sqlite3 --out /tmp/preview.json
python3 -m warehouse publish   --db /tmp/identity-rethread.sqlite3
python3 -m warehouse summary   --db /tmp/identity-rethread.sqlite3          # 当前发布代次
python3 -m warehouse summary   --db /tmp/identity-rethread.sqlite3 --generation 1   # 旧版查询
python3 -m warehouse mart      --db /tmp/identity-rethread.sqlite3 --generation 1
python3 -m warehouse facts     --db /tmp/identity-rethread.sqlite3
python3 -m warehouse dimensions --db /tmp/identity-rethread.sqlite3
python3 -m warehouse generations --db /tmp/identity-rethread.sqlite3
```

完整故事（连续合并后局部拆分、区间边界、未知成员补档、跨期改属、迟到合同，逐版校验金额守恒
与人数可解释）：

```bash
python3 scripts/demo.py           # 原有导入/查询/导出往返
python3 scripts/demo_reweave.py   # 重织端到端，28 项断言，失败返回非零退出码
```

两个演示都使用全新临时目录执行真实命令，不回放预制日志。手工重复使用数据库路径时应留意
已存在的业务主键。
