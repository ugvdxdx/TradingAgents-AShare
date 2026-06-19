#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fix batch6: 大族数控 & 协创数据"""
import json
with open('batch6_output.json') as f:
    b6 = json.load(f)

b6["301200"].update({
    "weaknesses":["极度依赖大族激光品牌和渠道：独立品牌力弱","PCB设备市场容量有限（全球约50亿美元）","与日本日立/三菱/德国Schmoll等国际巨头竞争","PCB设备周期性：PCB行业投资波动大","产品单一：仅PCB钻孔/成型/LDI曝光设备"],
    "growth_drivers":["AI服务器PCB：高阶PCB层数增加，激光钻孔需求爆发","IC载板（FC-BGA/FC-CSP）钻孔/激光加工","5G/6G通信基站PCB升级","汽车电子PCB：智能驾驶/域控制器PCB","东南亚/印度PCB产业转移：中国PCB设备出口"],
    "headwinds":["PCB行业投资增速放缓","PCB设备市场竞争加剧","AI服务器PCB设备技术门槛高","PCB产业向东南亚/印度转移减少国内需求","下游客户集中度高"],
    "geopolitical_risks":["中美贸易摩擦：PCB设备出口美国关税","日本/德国高端PCB设备对华出口管制","PCB产业链向东南亚/印度外移","全球PCB设备供应链区域化"],
    "geopolitical_opportunities":["中国PCB产业全球第一","中国PCB设备国产替代","一带一路PCB设备出口","中国在PCB/半导体设备领域的突破"]
})

b6["300857"].update({
    "weaknesses":["智能终端ODM毛利率极低（<15%）","与闻泰/华勤/龙旗等ODM巨头竞争","客户集中度高：小米/360等大客户营收占比高","AIoT产品线同质化严重","海外市场拓展慢"],
    "growth_drivers":["AIoT设备爆发：AI摄像头/智能门锁/智能音箱等","海外市场：东南亚/印度/南美等新兴市场","AI+IoT：大模型赋能的智能终端","数据存储/云服务：物联网终端产生海量数据","车联网/智能家居/智慧城市等新场景"],
    "headwinds":["智能终端ODM毛利率低","小米/360等大客户议价能力强","消费电子需求疲软","AIoT产品同质化竞争","海外市场政治风险"],
    "geopolitical_risks":["中美贸易摩擦：智能终端出口美国关税","全球消费电子供应链去中国化","印度/东南亚对中国ODM企业限制","数据安全/隐私法规对智能终端的影响"],
    "geopolitical_opportunities":["中国AIoT全球领先","全球智能终端ODM市场中国主导","一带一路智能终端市场","中国AI+IoT技术输出"]
})

with open('batch6_output.json', 'w') as f:
    json.dump(b6, f, ensure_ascii=False, indent=2)
print("Fixed batch6")