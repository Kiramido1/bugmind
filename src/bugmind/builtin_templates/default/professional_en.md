---
name: Professional QA Bug Report (English)
language: en
guidance: |
  - Write professional technical English.
  - title: "[Component] Clear and specific description of the defect, including actual vs expected behavior where appropriate". Concise, specific, technically meaningful.
  - description: detailed technical explanation: what the system currently does, what triggers it, relevant input values / boundary conditions / constraints, why the behavior is incorrect, user impact and potential backend/API impact, technical observations. Clearly separate confirmed facts from suspected root causes; qualify suspicions with "likely", "possibly", "suggests".
  - steps_to_reproduce: clear, numbered, actionable steps. Include required user role and initial conditions, exact input values and actions, DevTools/API inspection steps when applicable, and comparison tests when useful. Use ONLY information supplied by the user.
  - actual_result: observed behavior, exact HTTP status codes, response details, request payloads/response examples, frontend behavior and error messages, what fails or remains unsaved.
  - expected_result: correct behavior, appropriate HTTP status code when applicable, expected validation/error response structure when supported by existing API conventions, expected frontend behavior, security/data-integrity/regression requirements when relevant.
  - Preserve API paths, URLs, parameter names, payloads, response bodies and technical terms exactly. Use Markdown inline code for identifiers.
  - Never invent URLs, endpoints, HTTP codes, payloads or steps: leave the field null/empty so the report shows N/A.
  - Do not include proposed code fixes.
---
- **Bug Title:** {{ title | na }}

- **Environment & Endpoint Details:**
  - **Page URL:** {{ page_url | na }}
  - **API Endpoint:** {{ api_endpoints | join(", ") | na }}
  - **Request Method & Status Code:** {{ http_method | na }} — {{ actual_status_code | na }}

- **Description:**
{{ description | na | indent(2, true) }}

- **Steps to Reproduce:**
{{ steps_to_reproduce | numbered }}

- **Actual Result:**
{{ actual_result | bullets }}

- **Expected Result:**
{{ expected_result | bullets }}
