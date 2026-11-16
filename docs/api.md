# API naudojimas

Interaktyvi dokumentacija: http://localhost:3000/api/docs. Mašininiu būdu skaitoma specifikacija: `/api/openapi.json`. Visų žemiau pateiktų kelių pradžia – `/api`.

`POST` ir `PATCH` užklausos siunčiamos su `Content-Type: application/json`. Prisijungimas nustato `pulse_session` HttpOnly slapuką; tolesnės užklausos privalo jį siųsti. Naršyklėje reikalinga leistina kilmė, pagal nutylėjimą `http://localhost:8080`. Slaptažodis turi turėti bent 8 simbolius. Registracija visada sukuria narį.

Sąrašų atsakymai: `{"items": [...]}`, vienas objektas: `{"item": {...}}`, autentifikavimas: `{"user": {...}}`. Identifikatorius – `_id`, 24 šešioliktainiai simboliai. Susiję `trainerId`, `roomId`, `exerciseId`, `sessionId` laukai skaitymo atsakyme gali būti užpildyti objektais. Datoms su laiku naudokite ISO 8601 su laiko juosta, pavyzdžiui, `2026-12-01T16:00:00Z`.

| Metodas ir kelias | Prieiga | Paskirtis |
| --- | --- | --- |
| `POST /auth/register` | Vieša | `firstName`, `lastName`, `birthYear`, `email`, `password` |
| `POST /auth/login` | Vieša | `email`, `password`; nustato slapuką |
| `POST /auth/logout` | Vieša | Tuščias JSON `{}`; pašalina slapuką |
| `GET /auth/me` | Prisijungęs | Dabartinio vartotojo profilis |
| `GET /plans`, `/rooms`, `/exercises`, `/trainers` | Vieša | Aktyvūs katalogų įrašai; administratoriui `?all=true` rodo ir išjungtus |
| `POST /plans`, `/rooms`, `/exercises`, `/trainers` | Administratorius | Kurti katalogų įrašus |
| `PATCH /plans/{id}`, `/rooms/{id}`, `/exercises/{id}`, `/trainers/{id}` | Administratorius | Redaguoti; `{"isActive":false}` išjungia |
| `GET /memberships/me` | Prisijungęs | Savo narystės ir jų galiojimas |
| `POST /memberships` | Prisijungęs | `{"planId":"..."}`; imituojamas neribotos narystės pirkimas |
| `GET /sessions` | Vieša | Filtrai `from`, `to`, `date`, `trainerId`, `type`; iki 200 įrašų |
| `GET /sessions/{id}` | Vieša | Treniruotės duomenys |
| `POST /sessions` | Treneris / administratorius | Sukurti treniruotę; treneris gali kurti tik savo |
| `PATCH /sessions/{id}` | Savo treneris / administratorius | Redaguoti iki treniruotės pradžios |
| `DELETE /sessions/{id}` | Savo treneris / administratorius | Atšaukti kartu su registracijomis; istorija išsaugoma |
| `GET /sessions/{id}/bookings` | Savo treneris / administratorius | Dalyviai ir lankomumas |
| `GET /bookings/me` | Prisijungęs | Savo registracijų ir lankomumo istorija |
| `POST /bookings` | Narys | `{"sessionId":"..."}`; rezervuoti vietą |
| `DELETE /bookings/{id}` | Savininkas | Atšaukti registraciją iki treniruotės pradžios |
| `PATCH /bookings/{id}/attendance` | Savo treneris / administratorius | `{"status":"attended"}` arba `{"status":"no_show"}`; tik po treniruotės |
| `POST /reviews` | Dalyvavęs savininkas | `bookingId`, `rating` (1–5), `comment` |
| `GET /workout-logs` | Prisijungęs | Savo pratimų rezultatai |
| `POST /workout-logs` | Prisijungęs | Sukurti rezultatą |
| `PATCH /workout-logs/{id}` | Savininkas | Redaguoti rezultatą; `null` išvalo neprivalomą skaitinį lauką |
| `DELETE /workout-logs/{id}` | Savininkas | Ištrinti rezultatą |
| `GET /conversations` | Prisijungęs | Tik savo pokalbiai |
| `POST /conversations` | Narys | `{"trainerId":"..."}`; pradėti arba gauti esamą pokalbį |
| `GET /conversations/{id}/messages` | Pokalbio dalyvis | Naujausios 100 žinučių chronologine tvarka; `?before=nextCursor` senesnei istorijai |
| `POST /conversations/{id}/messages` | Pokalbio dalyvis | `{"content":"Labas!"}`; iki 4000 simbolių |
| `GET /analytics/summary` | Administratorius | `?from=2026-11-01&to=2026-11-30`; [rodiklių aprašymas](analytics.md) |

## Įrašų kūrimo pavyzdžiai

Narystės planas (kaina eurais; neribotas apsilankymų skaičius):

```json
{"name":"Mėnesio planas","price":39,"durationDays":30,"description":"Neriboti apsilankymai","isActive":true}
```

Salė:

```json
{"name":"Pagrindinė salė","capacity":20,"location":"1 aukštas","isActive":true}
```

Pratimas:

```json
{"name":"Pritūpimai","muscleGroup":"Kojos","category":"Jėga","description":"Pratimo aprašymas","isActive":true}
```

Treneris:

```json
{"firstName":"Jonas","lastName":"Jonaitis","birthYear":1995,"email":"jonas@example.com","password":"Sportas123!","specialization":"Jėgos treniruotės","isActive":true}
```

Treniruotė (`trainerId` treneriui neprivalomas; administratorius turi jį nurodyti):

```json
{"title":"Funkcinė jėga","type":"Funkcinė","description":"Viso kūno treniruotė","trainerId":"000000000000000000000001","roomId":"000000000000000000000002","startsAt":"2026-12-01T16:00:00Z","endsAt":"2026-12-01T17:00:00Z","capacity":16}
```

Žurnalo įrašas: reikia `sets` ir `repetitions` arba `durationSeconds`. `weight` – kilogramais, `durationSeconds` – sekundėmis.

```json
{"exerciseId":"000000000000000000000003","performedAt":"2026-12-01T17:00:00Z","sets":3,"repetitions":10,"weight":40,"notes":"Gera savijauta"}
```

Pavyzdžių identifikatorius pakeiskite tikrais, gautais iš sąrašų. `PATCH` užtenka siųsti tik keičiamus laukus.

## Klaidos ir vientisumas

Klaida pateikiama kaip `{"error":"Paaiškinimas"}`; validavimo klaida papildomai turi `details`. Kodai: `400` – netinkami duomenys, `401` – neprisijungęs, `403` – nepakankamos teisės, `404` – įrašas neprieinamas arba nerastas, `409` – pasikartojimas / talpos / laiko / būsenos konfliktas, `429` – užklausų limitas, `503` – analitika laikinai nepasiekiama.

Registracijos ir atšaukimo operacijos yra transakcinės. Atšaukimo pakartojimas papildomos vietos neatlaisvina. Į tą pačią treniruotę leidžiama registruotis vėl po atšaukimo, tačiau išsaugomas tas pats registracijos dokumentas ir analitinių įvykių istorija. Narystė visada turi apimti visą treniruotę.
