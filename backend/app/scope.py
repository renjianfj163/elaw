"""范围判断不交给模型。名单外的事项一律 OUT_OF_SCOPE。"""

IN_SCOPE = {
    "环评分类",
    "环评",
    "水污染",
    "医疗污水",
    "医疗废物",
    "危险废物",
    "辐射安全",
    "噪声",
    "大气",
    "锅炉",
    "备用发电机",
    "食堂油烟",
    "臭气",
    "排污许可",
    "竣工环保验收",
}

OUT_OF_SCOPE = {
    "规划许可",
    "用地",
    "消防",
    "医疗机构执业许可",
    "卫健部门审批",
    "地方性法规",
    "地方标准",
}


def judge_scope(topic: str) -> str:
    name = (topic or "").strip()
    if name in OUT_OF_SCOPE or name not in IN_SCOPE:
        return "OUT_OF_SCOPE"
    return "IN_SCOPE"
