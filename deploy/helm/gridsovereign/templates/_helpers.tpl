{{- define "gs.image" -}}{{ .root.Values.image.registry }}/{{ .name }}:{{ .root.Values.image.tag }}{{- end -}}
{{- define "gs.secEnv" -}}
- { name: GS_TOKEN_TRUST, value: /run/secrets/gs/trust.json }
{{- end -}}
{{- define "gs.podSecurity" -}}
securityContext: { runAsNonRoot: true, seccompProfile: { type: RuntimeDefault } }
{{- end -}}
{{- define "gs.containerSecurity" -}}
securityContext: { allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: { drop: [ALL] } }
{{- end -}}
