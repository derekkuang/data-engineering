{#
  Override dbt's default schema naming. By default dbt builds a model's schema
  as "<target.schema>_<custom_schema>" (e.g. crypto_staging_crypto_marts), which
  is convenient for multi-developer prod/dev isolation but here would create a
  Glue database our IAM policy doesn't grant. We use the custom schema verbatim
  so `+schema: crypto_marts` lands in exactly `crypto_marts`; models without a
  custom schema fall back to the profile's target schema (crypto_staging).
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
