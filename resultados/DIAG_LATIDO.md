# Diagnóstico del latido del bot
Generado: 17/10 20:32 (Caracas)

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

## Configuración del guardián

- LATIDO_TOPIC es igual a NTFY_TOPIC: **True**
- LATIDO_TOPIC termina en _estado: **False**
- LATIDO_TOPIC es igual a NTFY_TOPIC + '_estado': **False**

## Canal REAL del bot (NTFY_TOPIC + '_estado'): 59 mensajes en 12 h (HTTP 200)

### Huecos de más de 12 min en el canal REAL

- ninguno: el bot publicó sin cortes

### Copias del bot en el canal REAL

| versión | instancia | arrancó | primer estado | último estado |
|---|---|---|---|---|
| v7.1.2 | 047 | 17/10 10:13 | 17/10 10:13 | 17/10 20:28 |

Tamaño del estado REAL: mín 449 · máx 449 bytes · con adjunto: 0
Mensajes por hora: 4.9 (≈ 118 por día; el límite de ntfy.sh es 250 por IP)

### Qué cambia entre un estado y el siguiente (canal REAL)

- 58 veces: cap_t

## Canal de estado: 1 mensajes en 12 h (HTTP 200)

### Huecos de más de 12 min sin estado

- 17/10 09:16 → 17/10 20:32: **676 min**

### Copias del bot (instancias) que publicaron

| instancia | versión | arrancó | primer estado | último estado |
|---|---|---|---|---|
| None | None | ? | 17/10 09:16 | 17/10 09:16 |

Tamaño del estado: máx 143 bytes · con adjunto: 0 · ilegibles: 1

## Canal principal: 1 mensajes en 12 h

- guardián: no publica estado: 1 → 17/10 09:16
- guardián: volvió: 0
- app/bot: se apaga (otra copia): 0
- bot iniciado: 0
- error/falla: 0

## Detalle del canal de estado

| hora | min desde el anterior | bytes | adjunto | ilegible | versión | instancia |
|---|---|---|---|---|---|---|
| 17/10 09:16 |   0.0 | 143 |  | NO | None | None |
