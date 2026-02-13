{{/*
Expand the name of the chart.
*/}}
{{- define "bare-service-lb.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "bare-service-lb.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "bare-service-lb.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "bare-service-lb.labels" -}}
helm.sh/chart: {{ include "bare-service-lb.chart" . }}
{{ include "bare-service-lb.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "bare-service-lb.selectorLabels" -}}
app.kubernetes.io/name: {{ include "bare-service-lb.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "bare-service-lb.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "bare-service-lb.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Get the leader election lease name from environment variables
*/}}
{{- define "bare-service-lb.leaderElectionLeaseName" -}}
{{- $leaseName := "bare-service-lb-leader" -}}
{{- range .Values.env }}
{{- if eq .name "LEADER_ELECTION_LEASE_NAME" }}
{{- $leaseName = .value }}
{{- end }}
{{- end }}
{{- $leaseName }}
{{- end }}
