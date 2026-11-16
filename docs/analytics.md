# Analitika: MongoDB → ClickHouse → Redis

MongoDB yra operacinių duomenų šaltinis. ClickHouse kaupia tik analitinius
įvykius, o Redis iki 60 sekundžių saugo jau apskaičiuotas ataskaitas.
Visos narystės turi neribotą apsilankymų skaičių.

## Patikimas įvykių perdavimas

1. Backend MongoDB transakcijoje pakeičia verslo dokumentus ir įrašo `events`
   dokumentą. Nepavykus transakcijai, nei verslo pakeitimas, nei įvykis nelieka.
2. Python `start_event_worker()` kas 2 sekundes paima iki 100 neperduotų įvykių ir
   per ClickHouse HTTP sąsają įrašo `JSONEachRow` paketą.
3. Tik ClickHouse patvirtinus įrašymą, nustatomas MongoDB `publishedAt`.
4. Nepavykus perdavimui didinamas `attempts`, saugomas `lastError`; pakartojimų
   tarpas didėja iki 60 sekundžių. Įvykiai automatiškai neišmetami.

Worker paleidžiamas kartu su API. Atskirą papildomą worker procesą galima
paleisti iš `backend` katalogo komanda `python -m app.worker`, tačiau jis nėra būtinas.
Lygiagretūs worker procesai arba serverio lūžis po ClickHouse įrašo gali tą
patį paketą perduoti pakartotinai. Tai sąmoningas **at-least-once** perdavimas.

`analytics_events` naudoja `ReplacingMergeTree`, kurio raktas yra MongoDB
įvykio `_id`. Visos ataskaitų užklausos naudoja `FINAL`, todėl pakartotinai
perduotas įvykis suskaičiuojamas vieną kartą ir prieš foninį lentelės suliejimą.
Įvykiai nekintami; pasikeitusi verslo būsena sukuria naują įvykį.

## Įvykiai

| Tipas | Pagrindiniai `payload` laukai |
| --- | --- |
| `membership_purchased` | `membershipId`, `userId`, `planId`, `planName`, `pricePaid` |
| `booking_created` | `bookingId`, `sessionId`, `trainerId`, `sessionStartsAt`, `capacity`, `lifecycleVersion` |
| `booking_cancelled` | Tie patys registracijos duomenys, `reason`, `lifecycleVersion` |
| `attendance_marked` | Registracijos duomenys, `status`, `attendanceVersion` |
| `review_created` | Registracijos duomenys, `reviewId`, `rating` |
| `session_cancelled` | `sessionId`, `trainerId`, treniruotės laikas ir talpa |

Kartu perduodamas treniruotės pavadinimas ir trenerio vardas, kad įvykių
istorija išliktų suprantama. Slaptažodžiai, el. paštas, privačios žinutės ir
treniruočių žurnalo pastabos analitikai neperduodami.

Pakartotinė registracija po atšaukimo išlaiko tą patį `bookingId` ir padidina
`lifecycleVersion`. Ataskaita naudoja naujausią versiją, todėl atšaukta ir vėl
aktyvuota registracija teisingai užima vietą. Registracijų ir atšaukimų skaičius
skaičiuoja veiksmus: pakartotinis užsiregistravimas yra naujas registracijos
veiksmas, bet užimtumui narys suskaičiuojamas vieną kartą.

Pataisius lankomumo žymą, `attendanceVersion` leidžia parinkti naujausią
`attended` arba `no_show` reikšmę. Vienas narys negali būti suskaičiuotas abiejose
grupėse. Vienai registracijai leidžiamas vienas atsiliepimas.

## Ataskaitos

Tik administratoriui prieinamas:

```http
GET /api/analytics/summary?from=2026-11-01&to=2026-11-30
```

Datos be laiko interpretuojamos `Europe/Vilnius` laiko juostoje, įskaitant visą
paskutinę pasirinktą dieną ir vasaros / žiemos laiko pokyčius. Galima pateikti
ir pilną ISO datą su laiko juosta; tokiu atveju `to` yra neįtraukiama riba.
Numatytasis laikotarpis – šiandiena ir 29 ankstesnės kalendorinės dienos.

Narystės grupuojamos pagal **įsigijimo datą** ir pirkimo metu buvusio plano
pavadinimą bei sumokėtą kainą. Treniruotės grupuojamos pagal **treniruotės
pradžios datą**. Pradžios datą, dabartinį trenerį ir talpą ataskaita pasiima iš
MongoDB, o registracijų, lankomumo, atšaukimų ir įvertinimų agregatus apskaičiuoja
ClickHouse. Todėl prieš pradžią perkeltos treniruotės patenka į naują laikotarpį.

Atšauktos treniruotės neįtraukiamos į užimtumo, narių atšaukimų ir trenerių
lankomumo rodiklius. Dėl visos treniruotės atšaukimo atsiradę registracijų
atšaukimai (`reason = session_cancelled`) nėra nario atšaukimo veiksmai.
`totals.cancelledSessions` atskirai pateikia pasirinktu laikotarpiu turėjusių
vykti ir vėliau atšauktų treniruočių skaičių.

- Užimtumas = aktyvios registracijos / visų pasirinktų treniruočių vietos × 100.
  Būsimoms treniruotėms tai dabartinis užimtumas, pasibaigusioms – iki pradžios
  išlikusios registracijos. `attended` ir `no_show` registracijos abi užėmė vietą.
- Narių atšaukimų dalis = narių atšaukimo veiksmų skaičius / registracijos
  veiksmų skaičius × 100.
- Neatvykimų dalis = `no_show` / (`attended` + `no_show`) × 100.
  Dar nepažymėtos registracijos į vardiklį neįtraukiamos.
- Trenerio vidutinis įvertinimas = jo treniruočių įvertinimų suma / įvertinimų
  skaičius. Nesant įvertinimų grąžinama `null`.
- Bendras užimtumas sveriamas pagal vietų skaičių, o ne skaičiuojamas kaip
  atskirų treniruočių procentų vidurkis.

Visi atsakymo procentai yra intervale 0–100. Ataskaita pateikia `totals`,
`membershipsByPlan`, 20 populiariausių `popularSessions` ir `trainers` sąrašą.
`delivery.pendingEvents` parodo dar neperduotų įvykių skaičių. Rezultatai yra
eventual consistent: įprastas vėlavimas – worker intervalas ir iki 60 sekundžių
podėlyje. Nepasiekiamas Redis netrukdo skaičiuoti ataskaitos iš naujo.
Nepasiekiamas ClickHouse, kai nėra galiojančio podėlio, grąžina HTTP 503;
tušti ar išgalvoti analitikos rezultatai negrąžinami.

## Konfigūracija ir patikra

`CLICKHOUSE_URL`, `CLICKHOUSE_USER`, `CLICKHOUSE_PASSWORD`,
`CLICKHOUSE_DATABASE` nustatomi aplinkos kintamaisiais. Docker tinklo numatytoji
URL – `http://clickhouse:8123`; paleidus Python vietoje reikia
`http://localhost:8123`. Slaptažodis perduodamas HTTP antraštėje, o užklausų
reikšmės – tipizuotais ClickHouse parametrais.

Schema sukuriama `infra/clickhouse/init.sql`. Worker taip pat užtikrina, kad
konfigūruotoje duomenų bazėje egzistuoja lentelė, todėl galima pakeisti
numatytąjį duomenų bazės pavadinimą.

Įvykių eilės diagnostika:

```javascript
db.events.find({ publishedAt: null }, { type: 1, attempts: 1, lastError: 1 })
```

ClickHouse patikra:

```sql
SELECT event_type, count()
FROM gym.analytics_events FINAL
GROUP BY event_type;
```

Techniniai šaltiniai: [ClickHouse HTTP sąsaja](https://clickhouse.com/docs/interfaces/http),
[ReplacingMergeTree](https://clickhouse.com/docs/en/engines/table-engines/mergetree-family/replacingmergetree).

Automatinės analitikos patikros, naudojančios tikras MongoDB ir ClickHouse duomenų bazes:

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -p test_analytics.py -v
```

Patikros sukuria ir pašalina savo atskiras testavimo duomenų bazes. Jos apima
įvykio transakcijos atšaukimą, pakartotinį perdavimą, registracijos atšaukimą ir
atkūrimą, lankomumo pataisymą, podėlį, ClickHouse sutrikimą ir Lietuvos vasaros
bei žiemos laiko ribas.
