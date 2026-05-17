# Chart Schemas — bid-gen 渲染器图表 JSON 形状

本文档由 `bid_charts.py` 的 `_RENDERER_SCHEMAS` 自动生成。修改请改源,不要直接改本文件。

全部 7 种渲染器都已配 adapter 兼容层,LLM 可宽松写,adapter 自动归一化。

## 类型总览

| 类型 | 图标题 | 主要使用场景 |
|---|---|---|
| `schedule` | 施工进度横道图(Gantt) | 进度计划章节;附表四 进度网络图;任何含里程碑的工序时间表 |
| `org_chart` | 项目组织机构框图 | 项目组织管理机构章节;招标原文出现『框图』『组织机构』 |
| `hierarchy_flow` | 管理体系层级图 | 质量/安全/环保管理体系图;部门层级关系图 |
| `process_flow` | 工艺流程图 | 施工工艺流程;质量验收流程;材料检测流程;任何带方向/判断的步骤链 |
| `resource_curve` | 资源配置曲线/柱状图 | 劳动力投入曲线;资金使用计划;材料进退场曲线 |
| `aoa_network` | 双代号网络计划图(AOA/CPM) | 进度网络图(关键线路法);需要展示工序逻辑而非时间轴 |
| `site_layout` | 施工总平面布置图 | 招标原文要求『施工总平面图』『总平面布置图』;附表五 |

---

## `schedule` — 施工进度横道图(Gantt)

**使用场景:** 进度计划章节;附表四 进度网络图;任何含里程碑的工序时间表

**必填字段:** tasks

**全字段说明:**

- `tasks`:list of {name, start, end, phase}。start/end 接受 'YYYY-MM-DD' 或工期序号(int days from 0)。phase 用于横道颜色分组(可选)
- `milestones`:list of {name, date}(可选,菱形标记)
- `title`:图标题(可选,缺省走 _CHART_TITLES)

**最小可用 JSON:**

```json
{
  "tasks": [
    {
      "name": "施工准备",
      "start": "2026-05-31",
      "end": "2026-06-09",
      "phase": "准备"
    },
    {
      "name": "基础工程",
      "start": "2026-06-10",
      "end": "2026-06-30",
      "phase": "基础"
    },
    {
      "name": "主体结构",
      "start": "2026-07-01",
      "end": "2026-07-31",
      "phase": "主体"
    }
  ],
  "milestones": [
    {
      "name": "M1 基础验收",
      "date": "2026-06-30"
    },
    {
      "name": "M2 主体封顶",
      "date": "2026-07-31"
    }
  ]
}
```

---

## `org_chart` — 项目组织机构框图

**使用场景:** 项目组织管理机构章节;招标原文出现『框图』『组织机构』

**必填字段:** root

**全字段说明:**

- `root`:递归 {name|role, children:[]}。每个节点可有 children 数组
- `title`:图标题(可选)

**最小可用 JSON:**

```json
{
  "type": "org_chart",
  "root": {
    "name": "项目经理",
    "children": [
      {
        "name": "技术负责人",
        "children": [
          {
            "name": "质检员"
          },
          {
            "name": "试验员"
          }
        ]
      },
      {
        "name": "安全员",
        "children": [
          {
            "name": "文明施工员"
          }
        ]
      },
      {
        "name": "材料员"
      },
      {
        "name": "资料员"
      }
    ]
  }
}
```

---

## `hierarchy_flow` — 管理体系层级图

**使用场景:** 质量/安全/环保管理体系图;部门层级关系图

**必填字段:** layers

**全字段说明:**

- `layers`:list of {name(层名), nodes(成员名称数组)}。LLM 可写 list-of-list 由 adapter 自动转
- `connections`:list of {from, to, label}。可写 list-of-list `[[a,b]]`
- `title`:图标题(可选)

**最小可用 JSON:**

```json
{
  "type": "hierarchy_flow",
  "layers": [
    {
      "name": "决策层",
      "nodes": [
        "项目经理"
      ]
    },
    {
      "name": "管理层",
      "nodes": [
        "技术负责人",
        "安全负责人",
        "质量负责人"
      ]
    },
    {
      "name": "执行层",
      "nodes": [
        "施工班组A",
        "施工班组B"
      ]
    }
  ],
  "connections": [
    [
      "项目经理",
      "技术负责人"
    ],
    [
      "项目经理",
      "安全负责人"
    ],
    [
      "项目经理",
      "质量负责人"
    ],
    [
      "技术负责人",
      "施工班组A"
    ],
    [
      "技术负责人",
      "施工班组B"
    ]
  ]
}
```

---

## `process_flow` — 工艺流程图

**使用场景:** 施工工艺流程;质量验收流程;材料检测流程;任何带方向/判断的步骤链

**必填字段:** nodes, edges

**全字段说明:**

- `nodes`:list of {id, label, shape}。shape 可选 box/diamond/oval。LLM 可只写 list-of-string
- `edges`:list of {from, to, label}。可写 list-of-list `[[a,b],[b,c]]`
- `title`:图标题(可选)

**最小可用 JSON:**

```json
{
  "type": "process_flow",
  "nodes": [
    {
      "id": "s1",
      "label": "材料进场",
      "shape": "box"
    },
    {
      "id": "s2",
      "label": "现场抽检",
      "shape": "diamond"
    },
    {
      "id": "s3",
      "label": "合格入库",
      "shape": "box"
    },
    {
      "id": "s4",
      "label": "退场处理",
      "shape": "box"
    }
  ],
  "edges": [
    {
      "from": "s1",
      "to": "s2"
    },
    {
      "from": "s2",
      "to": "s3",
      "label": "合格"
    },
    {
      "from": "s2",
      "to": "s4",
      "label": "不合格"
    }
  ]
}
```

---

## `resource_curve` — 资源配置曲线/柱状图

**使用场景:** 劳动力投入曲线;资金使用计划;材料进退场曲线

**必填字段:** periods, series

**全字段说明:**

- `periods`:list of 时间标签(月/周/里程碑)
- `series`:list of {name, values}。LLM 可写 dict {name:values} 由 adapter 自动转
- `y_label`:纵轴单位(可选,如 '人' / '万元')
- `title`:图标题(可选)

**最小可用 JSON:**

```json
{
  "type": "resource_curve",
  "periods": [
    "6月",
    "7月",
    "8月",
    "9月"
  ],
  "series": [
    {
      "name": "普工",
      "values": [
        5,
        15,
        20,
        8
      ]
    },
    {
      "name": "焊工",
      "values": [
        0,
        10,
        15,
        5
      ]
    }
  ],
  "y_label": "人"
}
```

---

## `aoa_network` — 双代号网络计划图(AOA/CPM)

**使用场景:** 进度网络图(关键线路法);需要展示工序逻辑而非时间轴

**必填字段:** activities

**全字段说明:**

- `activities`:list of {from(int), to(int), name, duration}。from/to 是事件编号。LLM 写字符串 id 由 adapter 自动映射
- `events`:list of int,可选,缺省由 activities 推
- `title`:图标题(可选)

**最小可用 JSON:**

```json
{
  "type": "aoa_network",
  "activities": [
    {
      "from": 1,
      "to": 2,
      "name": "施工准备",
      "duration": 9
    },
    {
      "from": 2,
      "to": 3,
      "name": "基础",
      "duration": 21
    },
    {
      "from": 3,
      "to": 4,
      "name": "主体",
      "duration": 31
    },
    {
      "from": 4,
      "to": 5,
      "name": "围护机电",
      "duration": 35
    },
    {
      "from": 5,
      "to": 6,
      "name": "竣工",
      "duration": 14
    }
  ]
}
```

---

## `site_layout` — 施工总平面布置图

**使用场景:** 招标原文要求『施工总平面图』『总平面布置图』;附表五

**必填字段:** zones

**全字段说明:**

- `template`:rectangular_center | L_shape | U_shape,缺省 rectangular_center
- `main_building`:主体建筑名称(显示在中央)
- `zones`:dict {north,south,east,west: list of 设施名称}。LLM 写 list-of-{x,y,w,h,name} 由 adapter 按位置推方向
- `cranes`:list of {position(northeast_of_building 等), label}
- `title`:图标题(可选)

**最小可用 JSON:**

```json
{
  "type": "site_layout",
  "template": "rectangular_center",
  "main_building": "498㎡ 仓储中心",
  "zones": {
    "north": [
      "材料堆场",
      "钢构件预拼场"
    ],
    "south": [
      "办公生活区",
      "保安门卫"
    ],
    "east": [
      "机械停放",
      "汽车吊就位点"
    ],
    "west": [
      "临时变压器",
      "供水池"
    ]
  },
  "cranes": [
    {
      "position": "northeast_of_building",
      "label": "汽车吊 25t"
    }
  ]
}
```

---
