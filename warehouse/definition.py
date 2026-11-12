SCHEMA = {
    'identities': 'source_key TEXT PRIMARY KEY, entity TEXT NOT NULL',
    'contracts': 'contract TEXT PRIMARY KEY, source_key TEXT NOT NULL, amount TEXT NOT NULL, signed_on TEXT NOT NULL',
    # 上游拆分/合并决议：同一编号按版本递增，内容以摘要固定，重放幂等
    'resolutions': (
        'resolution_id TEXT NOT NULL, version INTEGER NOT NULL, '
        'digest TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT, '
        'PRIMARY KEY (resolution_id, version), UNIQUE (digest)'
    ),
    'resolution_entities': (
        'resolution_id TEXT NOT NULL, version INTEGER NOT NULL, entity TEXT NOT NULL, '
        'PRIMARY KEY (resolution_id, version, entity)'
    ),
    'resolution_mappings': (
        'resolution_id TEXT NOT NULL, version INTEGER NOT NULL, '
        'system TEXT NOT NULL, source_key TEXT NOT NULL, entity TEXT NOT NULL, '
        'valid_from TEXT NOT NULL, valid_to TEXT, '
        'PRIMARY KEY (resolution_id, version, system, source_key, valid_from)'
    ),
    # 统一实体维度：代理键单调分配，拆分实体只能取得新键，0 预留给未知维
    'dim_entity': 'surrogate INTEGER PRIMARY KEY, entity TEXT NOT NULL UNIQUE',
    # 可读代次：一次发布把维度版本、事实代理键、集市快照作为同一代切换
    'generations': (
        'generation INTEGER PRIMARY KEY, created_at TEXT NOT NULL, '
        'decision_fingerprint TEXT NOT NULL, base_fact_fingerprint TEXT NOT NULL'
    ),
    'generation_decisions': (
        'generation INTEGER NOT NULL, resolution_id TEXT NOT NULL, version INTEGER NOT NULL, '
        'PRIMARY KEY (generation, resolution_id)'
    ),
    # 既有事实的重织结果：保留原始来源身份与发生时间，仅追加按代次的代理键
    'fact_rw': (
        'generation INTEGER NOT NULL, contract TEXT NOT NULL, source_key TEXT NOT NULL, '
        'signed_on TEXT NOT NULL, month TEXT NOT NULL, surrogate INTEGER NOT NULL, amount TEXT NOT NULL, '
        'PRIMARY KEY (generation, contract)'
    ),
    # 集市：金额按合同累加，去重活跃客户按代理键去重，两者贡献规则不同
    'mart_rw': (
        'generation INTEGER NOT NULL, month TEXT NOT NULL, surrogate INTEGER NOT NULL, '
        'amount_sum TEXT NOT NULL, active_contracts INTEGER NOT NULL, '
        'PRIMARY KEY (generation, month, surrogate)'
    ),
    # 分区构建指纹：恢复/复用只接受输入与映射指纹都未变化的分区
    'partition_builds': (
        'generation INTEGER NOT NULL, month TEXT NOT NULL, '
        'input_fingerprint TEXT NOT NULL, mapping_fingerprint TEXT NOT NULL, '
        'PRIMARY KEY (generation, month)'
    ),
    'published_pointer': (
        'slot INTEGER PRIMARY KEY CHECK (slot = 1), generation INTEGER NOT NULL'
    ),
    # 发布互斥：两个发布者抢锁时只有一个能进入构建，输者整体重试，不暴露中间态
    'publication_locks': (
        'slot INTEGER PRIMARY KEY CHECK (slot = 1), holder TEXT NOT NULL, '
        'acquired_at TEXT NOT NULL'
    ),
}

# 未知成员固定落入未知维，补决议后的回填只能影响新一代次
UNKNOWN_ENTITY = '#unknown'
UNKNOWN_SURROGATE = 0
