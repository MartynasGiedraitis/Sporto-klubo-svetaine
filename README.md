# PULSE – sporto klubo sistema

Mokomasis projektas su **Python**, **FastAPI**, **MongoDB**, **Redis** ir **ClickHouse**. Visi narystės planai suteikia **neribotą apsilankymų skaičių** ir skiriasi kaina bei galiojimo trukme. Mokėjimas imituojamas.

## Paleidimas su Docker

Reikia veikiančio Docker Desktop su Linux konteineriais. Projekto aplanke:

```powershell
docker compose up --build -d
docker compose exec backend python -m app.seed
```

- Svetainė: http://localhost:8080
- API ir interaktyvi Swagger dokumentacija: http://localhost:3000/api/docs
- OpenAPI specifikacija: http://localhost:3000/api/openapi.json

Užklausų pavyzdžiai ir prieigos teisės: [API naudojimas](docs/api.md).

| Vaidmuo | El. paštas | Slaptažodis |
| --- | --- | --- |
| Narys | member@pulse.lt | Sportas123! |
| Treneris | trainer@pulse.lt | Sportas123! |
| Administratorius | admin@pulse.lt | Sportas123! |

Pradinių duomenų komanda sukuria planus, sales, pratimus, demonstracines paskyras ir treniruotes. Ją galima paleisti pakartotinai. Tai demonstracinės paskyros vietiniam projektui.

```powershell
docker compose logs -f backend frontend
docker compose down
```

`down` išsaugo duomenis Docker tomuose. `down -v` ištrina projekto duomenų bazes.

## Sandara

```text
Naršyklė: HTML + CSS, įprastos formos
                  │
                  ▼
Frontend: Python / FastAPI / Jinja2 (8080)
                  │ HTTP JSON API (httpx)
                  ▼
Backend: Python / FastAPI (3000)
       ├── MongoDB: operaciniai duomenys ir įvykių eilė
       ├── Redis: tvarkaraščio ir ataskaitų podėlis
       └── Foninis procesas → ClickHouse: analitiniai įvykiai
```

Frontend ir backend yra atskiri procesai ir konteineriai; frontend neturi tiesioginės prieigos prie duomenų bazių. Sąsaja sukurta be React ir be programos JavaScript. Formos perduodamos Python frontend serveriui, kuris iškviečia backend API. API taip pat galima naudoti savarankiškai iš Swagger ar kito kliento.

## Funkcijos

- Registracija, prisijungimas ir trys vaidmenys: narys, treneris, administratorius.
- Narystės planai, imituojamas pirkimas, galiojimo ir istorijos peržiūra.
- Treniruočių tvarkaraštis su datos, trenerio ir tipo filtrais.
- Registracija ir atšaukimas; narystė turi galioti **visą treniruotę**.
- Vienalaikių registracijų kontrolė: nedubliuojamos registracijos, neviršijama talpa, neleidžiami nario, trenerio ir salės laiko persidengimai.
- Trenerio treniruočių valdymas ir lankomumo žymėjimas po treniruotės.
- Vienas 1–5 balų atsiliepimas tik faktiškai dalyvavusiam nariui.
- Asmeninis pratimų žurnalas: data, serijos, pakartojimai, svoris arba trukmė, pastabos.
- Privatūs nario ir trenerio pokalbiai. Administratorius negauna prieigos prie svetimų pokalbių.
- Administratoriaus planų, trenerių, salių ir pratimų katalogų valdymas.
- ClickHouse ataskaitos apie narysčių pardavimus, registracijas, dalyvavimą, užimtumą, atšaukimus, neatvykimus ir trenerių įvertinimus.

## Duomenų modelis ir taisyklės

MongoDB kolekcijos: `users`, `membership_plans`, `memberships`, `training_sessions`, `bookings`, `rooms`, `exercises`, `workout_logs`, `conversations`, `messages`, `reviews`, `events`.

MongoDB veikia kaip vieno mazgo **replica set**, nes rezervacijų ir įvykių vientisumui naudojamos transakcijos. Vietos skaitiklio pakeitimas, registracijos įrašas ir analitinis įvykis patvirtinami kartu. Vartotojo, trenerio ir salės dokumentų versijų pakeitimai serializuoja konkuruojančias operacijas; vien tik išankstinės vietų patikros nepakanka.

Registraciją galima atšaukti iki treniruotės pradžios ir vėliau užsiregistruoti vėl, jei sąlygos vis dar tenkinamos. Atšaukus treniruotę, atšaukiamos jos registracijos. Salės talpa negali būti mažesnė už suplanuotų treniruočių dalyvių limitą. Planai ir katalogų įrašai išjungiami išsaugant istoriją. Vienas žurnalo įrašas aprašo vieną pratimą; serijoms taikomi tie patys pakartojimai ir svoris.

Analitiniai įvykiai: `membership_purchased`, `booking_created`, `booking_cancelled`, `attendance_marked`, `review_created`, `session_cancelled`. Įvykis į MongoDB įrašomas toje pačioje transakcijoje kaip veiksmas, o foninis procesas jį perduoda į ClickHouse. Pakartotinis pristatymas nedvigubina ataskaitų. Daugiau: [analitikos aprašymas](docs/analytics.md).

## Vietinis Python kūrimas

Python 3.12 ar naujesnis. Duomenų bazes paleiskite su Docker, programos komponentus – atskiruose terminaluose:

```powershell
docker compose up -d mongo mongo-init redis clickhouse
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

Backend terminale (MongoDB `directConnection=true` leidžia iš Windows prisijungti prie Docker replica set):

```powershell
$env:MONGODB_URI='mongodb://localhost:27017/pulse?replicaSet=rs0&directConnection=true'
$env:CLICKHOUSE_URL='http://localhost:8123'
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 3000 --reload
```

Frontend terminale:

```powershell
$env:API_URL='http://localhost:3000'
.\.venv\Scripts\python.exe -m uvicorn app:app --app-dir frontend --host 127.0.0.1 --port 8080 --reload
```

Pradiniams duomenims, jei backend paleistas lokaliai:

```powershell
$env:PYTHONPATH='backend'
$env:MONGODB_URI='mongodb://localhost:27017/pulse?replicaSet=rs0&directConnection=true'
.\.venv\Scripts\python.exe -m app.seed
```

## Patikrinimai

```powershell
.\.venv\Scripts\python.exe -m pytest -v
```

Integraciniai testai naudoja tikras vietines MongoDB, Redis ir ClickHouse tarnybas. Jie kuria atskiras testines duomenų bazes; demonstraciniai programos duomenys neliečiami. Testai tikrina vienalaikių rezervacijų vientisumą, prieigos teises ir analitikos skaičiavimus.

## Nustatymai

`.env.example` pateikia vietinio darbo reikšmes; tikras `.env` neįtraukiamas į Git. Docker Compose turi vietinio demonstravimo reikšmes, todėl galima pradėti be papildomo konfigūravimo. Viešam talpinimui būtini atskiri slaptažodžiai, ilgas atsitiktinis `JWT_SECRET`, HTTPS, saugūs slapukai ir apsaugota duomenų bazių prieiga. Duomenų bazių prievadai čia pasiekiami tik per `127.0.0.1`.

Pakeitimus galite išsaugoti esamoje Git repozitorijoje įprastai: `git add .` ir `git commit`. Priklausomybės, vietinė aplinka ir slapti nustatymai ignoruojami.
