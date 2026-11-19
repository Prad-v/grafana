{{- define "grafana-chat-agent.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "grafana-chat-agent.fullname" -}}
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

{{- define "grafana-chat-agent.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "grafana-chat-agent.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "grafana-chat-agent.selectorLabels" -}}
app.kubernetes.io/name: {{ include "grafana-chat-agent.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "grafana-chat-agent.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "grafana-chat-agent.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* The Secret that holds the shared token: the user's, or the one this chart creates. */}}
{{- define "grafana-chat-agent.authSecretName" -}}
{{- if .Values.auth.existingSecret -}}
{{- .Values.auth.existingSecret -}}
{{- else -}}
{{- printf "%s-auth" (include "grafana-chat-agent.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "grafana-chat-agent.anthropicSecretName" -}}
{{- if .Values.anthropic.existingSecret -}}
{{- .Values.anthropic.existingSecret -}}
{{- else -}}
{{- printf "%s-anthropic" (include "grafana-chat-agent.fullname" .) -}}
{{- end -}}
{{- end -}}
