{{- define "cognis.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- define "cognis.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "cognis.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- define "cognis.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
app.kubernetes.io/name: {{ include "cognis.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "cognis.selectorLabels" -}}
app.kubernetes.io/name: {{ include "cognis.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "cognis.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "cognis.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "cognis.image" -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) }}
{{- end }}

{{- define "cognis.validate" -}}
{{- if and (gt (int .Values.replicaCount) 1) (ne .Values.mode "ha") -}}
{{- fail "replicaCount > 1 requires mode=ha" -}}
{{- end -}}
{{- if .Values.migration.enabled -}}
{{- if ne .Values.database.type "postgresql" }}{{ fail "migration.enabled=true requires database.type=postgresql" }}{{ end -}}
{{- if not .Values.database.existingSecret }}{{ fail "migration.enabled=true requires database.existingSecret" }}{{ end -}}
{{- end -}}
{{- if eq .Values.mode "ha" -}}
{{- if lt (int .Values.replicaCount) 2 }}{{ fail "mode=ha requires replicaCount >= 2" }}{{ end -}}
{{- if ne .Values.database.type "postgresql" }}{{ fail "mode=ha requires database.type=postgresql" }}{{ end -}}
{{- if not .Values.database.existingSecret }}{{ fail "mode=ha requires database.existingSecret" }}{{ end -}}
{{- if not .Values.crypto.requireExternal }}{{ fail "mode=ha requires crypto.requireExternal=true" }}{{ end -}}
{{- if not .Values.crypto.existingSecret }}{{ fail "mode=ha requires crypto.existingSecret" }}{{ end -}}
{{- if ne .Values.artifacts.backend "s3" }}{{ fail "mode=ha requires artifacts.backend=s3" }}{{ end -}}
{{- if not .Values.artifacts.s3.existingSecret }}{{ fail "mode=ha requires artifacts.s3.existingSecret" }}{{ end -}}
{{- if ne .Values.toolOutputs.backend "s3" }}{{ fail "mode=ha requires toolOutputs.backend=s3" }}{{ end -}}
{{- if not .Values.toolOutputs.s3.existingSecret }}{{ fail "mode=ha requires toolOutputs.s3.existingSecret" }}{{ end -}}
{{- if not .Values.migration.enabled }}{{ fail "mode=ha requires migration.enabled=true" }}{{ end -}}
{{- if .Values.persistence.enabled }}{{ fail "mode=ha requires persistence.enabled=false" }}{{ end -}}
{{- if not .Values.podDisruptionBudget.enabled }}{{ fail "mode=ha requires podDisruptionBudget.enabled=true" }}{{ end -}}
{{- if not .Values.topologySpreadConstraints }}{{ fail "mode=ha requires topologySpreadConstraints" }}{{ end -}}
{{- end -}}
{{- end }}

{{- define "cognis.commonEnv" -}}
- name: COGNIS_DATA_DIR
  value: {{ .Values.config.dataDir | quote }}
- name: COGNIS_HOST
  value: "0.0.0.0"
- name: COGNIS_PORT
  value: {{ .Values.service.port | quote }}
- name: COGNIS_RUNTIME_MODE
  value: {{ ternary "ha" "simple" (eq .Values.mode "ha") | quote }}
- name: COGNIS_SCHEMA_MODE
  value: {{ ternary "validate" "auto" (or (eq .Values.mode "ha") .Values.migration.enabled) | quote }}
- name: COGNIS_REQUIRE_EXTERNAL_CRYPTO
  value: {{ .Values.crypto.requireExternal | quote }}
- name: COGNIS_MNEMORY_URL
  value: {{ .Values.config.mnemoryUrl | quote }}
- name: COGNIS_INTARIS_URL
  value: {{ .Values.config.intarisUrl | quote }}
- name: COGNIS_PUBLIC_BASE_URL
  value: {{ .Values.config.publicBaseUrl | quote }}
- name: COGNIS_LOG_LEVEL
  value: {{ .Values.config.logLevel | quote }}
- name: COGNIS_LOG_FORMAT
  value: {{ .Values.config.logFormat | quote }}
- name: COGNIS_TRUSTED_PROXY_CIDRS
  value: {{ .Values.config.trustedProxyCidrs | quote }}
- name: COGNIS_SHUTDOWN_DRAIN_TIMEOUT_SECONDS
  value: {{ .Values.shutdownDrainTimeoutSeconds | quote }}
- name: COGNIS_SHUTDOWN_CANCEL_TIMEOUT_SECONDS
  value: {{ .Values.shutdownCancelTimeoutSeconds | quote }}
- name: COGNIS_ARTIFACT_BACKEND
  value: {{ .Values.artifacts.backend | quote }}
- name: COGNIS_ARTIFACT_PATH
  value: {{ .Values.artifacts.filesystemPath | quote }}
- name: COGNIS_TOOL_OUTPUT_BACKEND
  value: {{ .Values.toolOutputs.backend | quote }}
{{- if .Values.database.existingSecret }}
- name: DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.database.existingSecret | quote }}
      key: {{ .Values.database.secretKey | quote }}
{{- end }}
{{- if .Values.redis.existingSecret }}
- name: COGNIS_REDIS_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.redis.existingSecret | quote }}
      key: {{ .Values.redis.secretKey | quote }}
{{- end }}
- name: COGNIS_EVENT_CACHE_TTL_SECONDS
  value: {{ int .Values.redis.eventCache.ttlSeconds | quote }}
- name: COGNIS_EVENT_CACHE_SLIDING_TTL
  value: {{ .Values.redis.eventCache.slidingTtl | quote }}
- name: COGNIS_EVENT_CACHE_COMPRESSION_ENABLED
  value: {{ .Values.redis.eventCache.compressionEnabled | quote }}
- name: COGNIS_EVENT_CACHE_COMPRESSION_THRESHOLD_BYTES
  value: {{ int .Values.redis.eventCache.compressionThresholdBytes | quote }}
- name: COGNIS_EVENT_CACHE_MAX_VALUE_BYTES
  value: {{ int .Values.redis.eventCache.maxValueBytes | quote }}
{{- if eq .Values.artifacts.backend "s3" }}
- name: COGNIS_ARTIFACT_S3_ENDPOINT
  value: {{ .Values.artifacts.s3.endpoint | quote }}
- name: COGNIS_ARTIFACT_S3_BUCKET
  value: {{ .Values.artifacts.s3.bucket | quote }}
- name: COGNIS_ARTIFACT_S3_REGION
  value: {{ .Values.artifacts.s3.region | quote }}
- name: COGNIS_ARTIFACT_S3_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.artifacts.s3.existingSecret | quote }}
      key: {{ .Values.artifacts.s3.accessKeyKey | quote }}
- name: COGNIS_ARTIFACT_S3_SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.artifacts.s3.existingSecret | quote }}
      key: {{ .Values.artifacts.s3.secretKeyKey | quote }}
- name: COGNIS_ARTIFACT_SIGNING_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ .Values.artifacts.s3.existingSecret | quote }}
      key: {{ .Values.artifacts.s3.signingSecretKey | quote }}
{{- end }}
{{- if eq .Values.toolOutputs.backend "s3" }}
- name: COGNIS_TOOL_OUTPUT_S3_ENDPOINT
  value: {{ .Values.toolOutputs.s3.endpoint | quote }}
- name: COGNIS_TOOL_OUTPUT_S3_BUCKET
  value: {{ .Values.toolOutputs.s3.bucket | quote }}
- name: COGNIS_TOOL_OUTPUT_S3_REGION
  value: {{ .Values.toolOutputs.s3.region | quote }}
- name: COGNIS_TOOL_OUTPUT_S3_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.toolOutputs.s3.existingSecret | quote }}
      key: {{ .Values.toolOutputs.s3.accessKeyKey | quote }}
- name: COGNIS_TOOL_OUTPUT_S3_SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.toolOutputs.s3.existingSecret | quote }}
      key: {{ .Values.toolOutputs.s3.secretKeyKey | quote }}
{{- end }}
{{- with .Values.config.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end }}
