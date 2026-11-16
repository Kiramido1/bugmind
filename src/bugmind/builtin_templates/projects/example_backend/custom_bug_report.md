---
name: Example backend project template (compact)
language: en
guidance: |
  - Compact report for API-only defects. Keep each section short but keep all technical evidence verbatim.
---
### {{ title | na }}

| Field | Value |
|---|---|
| Component | {{ component | na }} |
| Endpoint | {{ api_endpoints | join(", ") | na }} |
| Method / Status | {{ http_method | na }} — {{ actual_status_code | na }} (expected {{ expected_status_code | na }}) |
| Severity | {{ severity }} |

{{ description | na }}

**Steps**
{{ steps_to_reproduce | numbered }}

**Actual**
{{ actual_result | bullets }}

**Expected**
{{ expected_result | bullets }}
{% if evidence %}

**Evidence**
{% for e in evidence %}
```
{{ e }}
```
{% endfor %}
{% endif %}
