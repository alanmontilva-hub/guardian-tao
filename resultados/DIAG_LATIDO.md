# Diagnóstico del latido del bot
Generado: 17/10 10:08 (Caracas)

## Límites de ntfy.sh para un visitante anónimo (visto desde GitHub)
```
{
 "tier": null,
 "limits": {
  "basis": "ip",
  "messages": 250,
  "messages_expiry_duration": 43200,
  "emails": 5,
  "calls": 0,
  "reservations": 0,
  "attachment_total_size": 20971520,
  "attachment_file_size": 2097152,
  "attachment_expiry_duration": 10800,
  "attachment_bandwidth": 367001600
 }
}
```

## Canal de estado: 3 mensajes en 12 h (HTTP 200)

### Huecos de más de 12 min sin estado

- 17/10 01:41 → 17/10 05:44: **243 min**
- 17/10 05:44 → 17/10 09:16: **213 min**
- 17/10 09:16 → 17/10 10:08: **52 min**

### Copias del bot (instancias) que publicaron

| instancia | versión | arrancó | primer estado | último estado |
|---|---|---|---|---|
| None | None | ? | 17/10 01:41 | 17/10 09:16 |

Tamaño del estado: máx 143 bytes · con adjunto: 0 · ilegibles: 3

## Canal principal: 3 mensajes en 12 h

- guardián: no publica estado: 3 → 17/10 01:41, 17/10 05:44, 17/10 09:16
- guardián: volvió: 0
- app/bot: se apaga (otra copia): 0
- bot iniciado: 0
- error/falla: 0

## Detalle del canal de estado

| hora | min desde el anterior | bytes | adjunto | ilegible | versión | instancia |
|---|---|---|---|---|---|---|
| 17/10 01:41 |   0.0 | 143 |  | NO | None | None |
| 17/10 05:44 | 243.1 | 143 |  | NO | None | None |
| 17/10 09:16 | 212.6 | 143 |  | NO | None | None |
