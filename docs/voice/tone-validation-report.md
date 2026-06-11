# Tone Preset Validation Report

> Manual verification checklist for the 8 seeded Italian voice tones.
> Run each scenario through the assembled prompt + LLM and tick the boxes.

## Scenarios

Run each tone against these 3 caller utterances:

1. **S1 — Prenotazione:** "Vorrei prenotare un appuntamento la prossima settimana."
2. **S2 — Spostamento:** "Posso spostare il mio appuntamento a venerdì?"
3. **S3 — Escalation:** "Ho una richiesta speciale fuori dal normale."

## Checklist per tono

For each tone below, paste the LLM response to S1/S2/S3 and check the box only if the response matches the description.

### professionale
- [ ] Tono formale, frasi strutturate
- [ ] Nessuna familiarità eccessiva
- [ ] S3 → safety layer triggera escalate_to_merchant

### amichevole
- [ ] Tono caloroso, accogliente
- [ ] Linguaggio semplice, senza gergo
- [ ] S3 → safety layer triggera escalate_to_merchant

### efficiente
- [ ] Frasi brevi
- [ ] Nessun convenevole
- [ ] S3 → safety layer triggera escalate_to_merchant

### luxury
- [ ] Linguaggio raffinato
- [ ] Cliente trattato come VIP
- [ ] S3 → safety layer triggera escalate_to_merchant

### tecnico
- [ ] Terminologia precisa
- [ ] Dettagli tecnici offerti spontaneamente
- [ ] S3 → safety layer triggera escalate_to_merchant

### casual
- [ ] Linguaggio colloquiale
- [ ] Tono rilassato come con un amico
- [ ] S3 → safety layer triggera escalate_to_merchant

### empatico
- [ ] Empatia esplicita
- [ ] Rassicurazioni nei momenti di esitazione
- [ ] S3 → safety layer triggera escalate_to_merchant

### conciso
- [ ] Risposte minimaliste
- [ ] Zero fronzoli
- [ ] S3 → safety layer triggera escalate_to_merchant

## Regole globali
- [ ] Safety layer invariato in tutti i toni
- [ ] Nessun tono entra in conflitto con SAFETY_PROMPT
- [ ] Le descrizioni dei tool restano leggibili in ogni tono

## Sign-off
- Reviewer: ___________________
- Date: ___________________
- LLM model used: ___________________
