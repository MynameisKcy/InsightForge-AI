# {{ title }}

**生成时间**: {{ generated_at }}

---

## 一、执行摘要

{{ executive_summary }}

---

## 二、总体趋势分析

{% if trend_error %}
> ⚠️ {{ trend_error }}
{% else %}
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
{% endif %}

---

## 三、分组对比分析

{% if product_error %}
> ⚠️ {{ product_error }}
{% else %}
{{ product_insight }}

{% if top_products %}
### TOP 项

| 排名 | {{ dimension_label }} | {{ measure_label }} |
|------|------|--------|
{% for p in top_products %}
| {{ loop.index }} | {{ p[dimension_col] }} | {{ p[measure_col] }} |
{% endfor %}
{% endif %}

{% if category_summary %}
### 分组占比

| {{ category_label }} | {{ measure_label }} | 占比 |
|------|--------|------|
{% for c in category_summary %}
| {{ c[category_col] }} | {{ c[measure_col] }} | {{ c.revenue_pct }}% |
{% endfor %}
{% endif %}

{% if product_chart %}
![分组对比图]({{ product_chart }})
{% endif %}
{% endif %}

---

## 四、风险分析

{% if risk_error %}
> ⚠️ {{ risk_error }}
{% else %}
**风险等级**: {{ risk_level }}

{{ risk_assessment }}

{% if key_risks %}
### 主要风险点
{% for risk in key_risks %}
- {{ risk }}
{% endfor %}
{% endif %}

{% if measure_anomalies %}
### 度量异常时段
{% for a in measure_anomalies.anomaly_months %}
- {{ a.month }}: 值 {{ a.value }} (IQR异常: {{ a.iqr_flag }}, Z-score异常: {{ a.zscore_flag }})
{% endfor %}
{% endif %}
{% endif %}

---

## 五、建议

{{ recommendations }}

---

## 六、结论

{{ conclusion }}

---

*本报告由 InsightForge AI 多智能体数据分析系统自动生成*
