# {{ title }}

**生成时间**: {{ generated_at }}

---

## 一、执行摘要

{{ executive_summary }}

---

## 二、总体趋势分析

{{ trend_insight }}

### 关键指标

| 指标 | 数值 |
|------|------|
| 整体趋势方向 | {{ direction }} |
| 总体变化幅度 | {{ overall_growth_pct }}% |
| 起始值 | {{ start_value }} |
| 结束值 | {{ end_value }} |

{% if anomaly_months %}
### 异常月份

{{ anomaly_months_detail }}
{% endif %}

{% if trend_chart %}
![趋势图]({{ trend_chart }})
{% endif %}

---

## 三、产品分析

{{ product_insight }}

### TOP 产品

| 排名 | 产品 | 总收入 | 销量 | 订单数 |
|------|------|--------|------|--------|
{% for p in top_products %}
| {{ loop.index }} | {{ p.Product_Description or p.product }} | {{ p.total_revenue }} | {{ p.total_quantity }} | {{ p.order_count }} |
{% endfor %}

### 类别分析

| 类别 | 总收入 | 收入占比 | 销量 | 产品数 |
|------|--------|----------|------|--------|
{% for c in category_summary %}
| {{ c.Product_Category or c.category }} | {{ c.total_revenue }} | {{ c.revenue_pct }}% | {{ c.total_quantity }} | {{ c.product_variety }} |
{% endfor %}

{% if product_chart %}
![产品分析图]({{ product_chart }})
{% endif %}

---

## 四、风险分析

**风险等级**: {{ risk_level }}

{{ risk_assessment }}

{% if key_risks %}
### 主要风险点
{% for risk in key_risks %}
- {{ risk }}
{% endfor %}
{% endif %}

{% if revenue_anomalies %}
### 收入异常月份
{% for a in revenue_anomalies.anomaly_months %}
- 月份 {{ a.month }}: 收入 {{ a.revenue }} (IQR异常: {{ a.iqr_flag }}, Z-score异常: {{ a.zscore_flag }})
{% endfor %}
{% endif %}

---

## 五、建议

{{ recommendations }}

---

## 六、结论

{{ conclusion }}

---

*本报告由 AI Data Analyst Multi-Agent System 自动生成*
