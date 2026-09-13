{{/* vim: set filetype=mustache: */}}

{{- define "loomvec.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "loomvec.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "loomvec.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "loomvec.labels" -}}
helm.sh/chart: {{ include "loomvec.chart" . }}
{{ include "loomvec.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- with .Values.extraLabels }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{- define "loomvec.selectorLabels" -}}
app.kubernetes.io/name: {{ include "loomvec.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "loomvec.componentLabels" -}}
{{ include "loomvec.selectorLabels" .context }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "loomvec.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "loomvec.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- /* 应用配置 Secret 名（existingSecret 优先） */ -}}
{{- define "loomvec.secretName" -}}
{{- default (printf "%s-config" (include "loomvec.fullname" .)) .Values.secret.existingSecret -}}
{{- end -}}

{{- /* 镜像引用（global.imageRegistry 前缀 + 默认 tag = AppVersion） */ -}}
{{- define "loomvec.image" -}}
{{- $root := .root -}}
{{- $img := .image -}}
{{- $registry := $root.Values.global.imageRegistry -}}
{{- $tag := default $root.Chart.AppVersion $img.tag -}}
{{- if $registry -}}
{{- printf "%s/%s:%s" $registry $img.repository $tag -}}
{{- else -}}
{{- printf "%s:%s" $img.repository $tag -}}
{{- end -}}
{{- end -}}

{{- /* PostgreSQL DSN：existingSecret 优先，否则 values 内 url */ -}}
{{- define "loomvec.postgresUrlEnv" -}}
{{- if .Values.postgres.existingSecret -}}
- name: LOOMVEC_POSTGRES__URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.postgres.existingSecret }}
      key: {{ .Values.postgres.existingSecretKey }}
{{- else -}}
- name: LOOMVEC_POSTGRES__URL
  value: {{ .Values.postgres.url | quote }}
{{- end -}}
{{- end -}}

{{- /* Redis URL */ -}}
{{- define "loomvec.redisUrlEnv" -}}
{{- if .Values.redis.existingSecret -}}
- name: LOOMVEC_REDIS__URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.redis.existingSecret }}
      key: {{ .Values.redis.existingSecretKey }}
{{- else -}}
- name: LOOMVEC_REDIS__URL
  value: {{ .Values.redis.url | quote }}
{{- end -}}
{{- end -}}

{{- /* Milvus URI：子 chart 启用时指向其内部服务 */ -}}
{{- define "loomvec.milvusUri" -}}
{{- if .Values.milvus.enabled -}}
http://{{ include "loomvec.fullname" . }}-milvus:19530
{{- else -}}
{{ .Values.milvus.external.uri }}
{{- end -}}
{{- end -}}

{{- /* 对象存储 endpoint：internal → 内置 RustFS；external → 显式地址 */ -}}
{{- define "loomvec.storageEndpoint" -}}
{{- if eq .Values.storage.mode "external" -}}
{{ .Values.storage.external.endpoint }}
{{- else -}}
http://{{ include "loomvec.fullname" . }}-rustfs:{{ .Values.storage.internal.port }}
{{- end -}}
{{- end -}}

{{- /* 对象存储凭据 secretKeyRef */ -}}
{{- /* 外置存储凭据：显式 accessKey/secretKey 优先，否则走 Secret */ -}}
{{- define "loomvec.storageAccessKeyEnv" -}}
{{- if and (eq .Values.storage.mode "external") .Values.storage.external.accessKey -}}
- name: LOOMVEC_STORAGE__ACCESS_KEY
  value: {{ .Values.storage.external.accessKey | quote }}
{{- else if eq .Values.storage.mode "external" -}}
- name: LOOMVEC_STORAGE__ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ default (include "loomvec.secretName" .) .Values.storage.external.existingSecret }}
      key: {{ .Values.storage.external.accessKeyKey }}
{{- else -}}
- name: LOOMVEC_STORAGE__ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "loomvec.secretName" . }}
      key: storage-access-key
{{- end -}}
{{- end -}}

{{- define "loomvec.storageSecretKeyEnv" -}}
{{- if and (eq .Values.storage.mode "external") .Values.storage.external.secretKey -}}
- name: LOOMVEC_STORAGE__SECRET_KEY
  value: {{ .Values.storage.external.secretKey | quote }}
{{- else if eq .Values.storage.mode "external" -}}
- name: LOOMVEC_STORAGE__SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: {{ default (include "loomvec.secretName" .) .Values.storage.external.existingSecret }}
      key: {{ .Values.storage.external.secretKeyKey }}
{{- else -}}
- name: LOOMVEC_STORAGE__SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "loomvec.secretName" . }}
      key: storage-secret-key
{{- end -}}
{{- end -}}

{{/*
应用公共环境变量列表（api / worker / migrate 共用）。
静态项在前，依赖派生项在后（同名时后者生效）。
*/}}
{{- define "loomvec.env" -}}
{{- $root := . -}}
- name: LOOMVEC_ENV
  value: {{ $root.Values.config.env | quote }}
- name: LOOMVEC_LOG_LEVEL
  value: {{ $root.Values.config.logLevel | quote }}
- name: LOOMVEC_LOG_JSON
  value: {{ $root.Values.config.logJson | toString | quote }}
{{- range $k, $v := $root.Values.config.extraEnv }}
- name: LOOMVEC_{{ $k }}
  value: {{ $v | toString | quote }}
{{- end }}
{{- range $k, $v := $root.Values.config.ai.extra }}
- name: LOOMVEC_AI__{{ $k }}
  value: {{ $v | toString | quote }}
{{- end }}
- name: LOOMVEC_AI__MOCK
  value: {{ $root.Values.config.ai.mock | toString | quote }}
{{- if $root.Values.config.mineru.enabled }}
- name: LOOMVEC_MINERU__BASE_URL
  value: {{ $root.Values.config.mineru.baseUrl | quote }}
- name: LOOMVEC_MINERU__BACKEND
  value: {{ $root.Values.config.mineru.backend | quote }}
{{- end }}
- name: LOOMVEC_AUTH__DEV_MODE
  value: {{ $root.Values.config.auth.devMode | toString | quote }}
- name: LOOMVEC_AUTH__JWT_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ include "loomvec.secretName" $root }}
      key: auth-jwt-secret
- name: LOOMVEC_STORAGE__ENDPOINT
  value: {{ include "loomvec.storageEndpoint" $root | quote }}
- name: LOOMVEC_STORAGE__BUCKET_RAW
  value: {{ $root.Values.config.storage.bucketRaw | quote }}
- name: LOOMVEC_STORAGE__BUCKET_DERIVED
  value: {{ $root.Values.config.storage.bucketDerived | quote }}
{{ include "loomvec.storageAccessKeyEnv" $root }}
{{ include "loomvec.storageSecretKeyEnv" $root }}
- name: LOOMVEC_MILVUS__URI
  value: {{ include "loomvec.milvusUri" $root | quote }}
{{ include "loomvec.postgresUrlEnv" $root }}
{{ include "loomvec.redisUrlEnv" $root }}
{{- range $k, $v := $root.Values.secret.stringData }}
- name: {{ $k }}
  valueFrom:
    secretKeyRef:
      name: {{ include "loomvec.secretName" $root }}
      key: {{ $k }}
{{- end }}
{{- end -}}

{{- /* 滚动更新策略（maxSurge 1 / maxUnavailable 0 或 OnDelete） */ -}}
{{- define "loomvec.updateStrategy" -}}
{{- if eq .type "OnDelete" -}}
type: OnDelete
{{- else -}}
type: RollingUpdate
rollingUpdate:
  maxSurge: {{ .maxSurge }}
  maxUnavailable: {{ .maxUnavailable }}
{{- end -}}
{{- end -}}
