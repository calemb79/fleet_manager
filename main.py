from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime, date, timedelta
from typing import Optional
import motor.motor_asyncio
from bson import ObjectId
import hashlib
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import smtplib
from email.mime.text import MIMEText
from email.header import Header
import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler

app = FastAPI()

# --- Konfiguracja ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MONGODB_URL = "mongodb+srv://MACIEJ:20250811@cluster0.nkzhycg.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URL)
db = client.fleet_management

security = HTTPBasic()

EMAIL_CONFIG = {
    "SMTP_SERVER": "smtp.mail.ovh.net",
    "SMTP_PORT": 465,
    "SMTP_USER": "logistyka@bestem.ovh",
    "SMTP_PASSWORD": "Teneryfa25!",
    "FROM": "logistyka@bestem.ovh",
}

# --- Harmonogram do automatycznych powiadomień ---
scheduler = AsyncIOScheduler(timezone="Europe/Warsaw")


# --- Helpers ---
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def ensure_objectid(id_or_obj):
    return id_or_obj if isinstance(id_or_obj, ObjectId) else ObjectId(id_or_obj)


def format_date_ymd(dt: datetime) -> str:
    return dt.date().isoformat()


async def authenticate_user(username: str, password: str):
    user = await db.users.find_one({"username": username})
    if not user or user["password"] != hash_password(password):
        return False
    return {"_id": user["_id"], "username": user["username"], "full_name": user["full_name"]}


# --- Modele ---
class VehicleCreate(BaseModel):
    name: str
    secondary_name: Optional[str] = None
    registration_number: str
    vin: str
    inspection_date: str
    insurance_date: str
    assigned_user_id: Optional[str] = None
    email: str
    notification_period: int
    notes: Optional[str] = None


class LoginForm(BaseModel):
    username: str
    password: str


# --- Logika biznesowa (e-mail, CRUD) ---
async def send_email_async(subject: str, html_body: str, to_email: str, bcc_email: Optional[str] = None):
    msg = MIMEText(html_body, "html", _charset="utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = EMAIL_CONFIG["FROM"]
    msg["To"] = to_email

    recipients = [to_email]
    if bcc_email:
        msg["Bcc"] = bcc_email
        recipients.append(bcc_email)

    def _send():
        with smtplib.SMTP_SSL(EMAIL_CONFIG["SMTP_SERVER"], EMAIL_CONFIG["SMTP_PORT"]) as server:
            server.login(EMAIL_CONFIG["SMTP_USER"], EMAIL_CONFIG["SMTP_PASSWORD"])
            server.sendmail(EMAIL_CONFIG["FROM"], recipients, msg.as_string())

    await asyncio.to_thread(_send)


# --- NOWA FUNKCJA: Logika automatycznych powiadomień ---
async def check_vehicle_expirations():
    print(f"[{datetime.now()}] Uruchamianie zadania sprawdzającego terminy...")
    today = date.today()

    async for vehicle in db.vehicles.find({}):
        try:
            vehicle_id = vehicle["_id"]
            period = timedelta(days=vehicle.get("notification_period", 30))
            email_to = vehicle.get("email")

            if not email_to:
                continue

            # 1. Sprawdzenie terminu przeglądu
            insp_date = vehicle["inspection_date"].date()
            notify_on_insp = insp_date - period

            if today == notify_on_insp:
                # Sprawdź, czy już wysłano powiadomienie dla tej daty przeglądu
                if vehicle.get("inspection_notified_for_date") != str(insp_date):
                    subject = f"Przypomnienie: Zbliża się termin przeglądu dla {vehicle['name']}"
                    html_body = f"""
                    Witaj,<br><br>
                    System przypomina o zbliżającym się terminie przeglądu technicznego dla pojazdu:
                    <b>{vehicle['name']} ({vehicle['registration_number']})</b>.<br>
                    Termin upływa dnia: <b>{insp_date.isoformat()}</b>.<br><br>
                    Pozdrawiamy,<br>Zespół Floty
                    """
                    await send_email_async(subject, html_body, email_to, "flota@bestem.pl")
                    # Zaktualizuj flagę w bazie, aby nie wysyłać ponownie
                    await db.vehicles.update_one(
                        {"_id": vehicle_id},
                        {"$set": {"inspection_notified_for_date": str(insp_date)}}
                    )
                    print(f"Wysłano powiadomienie o przeglądzie dla pojazdu: {vehicle['name']}")

            # 2. Sprawdzenie terminu ubezpieczenia
            insu_date = vehicle["insurance_date"].date()
            notify_on_insu = insu_date - period

            if today == notify_on_insu:
                # Sprawdź, czy już wysłano powiadomienie dla tej daty ubezpieczenia
                if vehicle.get("insurance_notified_for_date") != str(insu_date):
                    subject = f"Przypomnienie: Zbliża się termin ubezpieczenia dla {vehicle['name']}"
                    html_body = f"""
                    Witaj,<br><br>
                    System przypomina o zbliżającym się terminie ważności ubezpieczenia OC/AC dla pojazdu:
                    <b>{vehicle['name']} ({vehicle['registration_number']})</b>.<br>
                    Termin upływa dnia: <b>{insu_date.isoformat()}</b>.<br><br>
                    Pozdrawiamy,<br>Zespół Floty
                    """
                    await send_email_async(subject, html_body, email_to, "flota@bestem.pl")
                    # Zaktualizuj flagę w bazie
                    await db.vehicles.update_one(
                        {"_id": vehicle_id},
                        {"$set": {"insurance_notified_for_date": str(insu_date)}}
                    )
                    print(f"Wysłano powiadomienie o ubezpieczeniu dla pojazdu: {vehicle['name']}")

        except Exception as e:
            print(f"Błąd podczas przetwarzania pojazdu {vehicle.get('_id')}: {e}")


# --- API Endpoints ---
@app.on_event("startup")
async def startup_event():
    # Uruchom zadanie codziennie o 8:00 rano
    scheduler.add_job(check_vehicle_expirations, 'cron', hour=8, minute=0)
    scheduler.start()
    print("Harmonogram zadań został uruchomiony.")


@app.on_event("shutdown")
async def shutdown_event():
    scheduler.shutdown()
    print("Harmonogram zadań został zatrzymany.")


@app.post("/api/login")
async def login(form_data: LoginForm):
    user = await authenticate_user(form_data.username, form_data.password)
    if not user:
        raise HTTPException(status_code=400, detail="Incorrect username or password")
    return {"message": "Login successful",
            "user": {"username": user["username"], "full_name": user["full_name"], "id": str(user["_id"])}}


@app.post("/api/vehicles")
async def create_vehicle(vehicle: VehicleCreate, credentials: HTTPBasicCredentials = Depends(security)):
    user = await authenticate_user(credentials.username, credentials.password)
    if not user: raise HTTPException(status_code=401, detail="Unauthorized")

    vehicle_data = vehicle.dict()
    vehicle_data["inspection_date"] = datetime.strptime(vehicle.inspection_date, "%Y-%m-%d")
    vehicle_data["insurance_date"] = datetime.strptime(vehicle.insurance_date, "%Y-%m-%d")
    vehicle_data["assigned_user_id"] = user["_id"]
    # Inicjalizacja pól do śledzenia powiadomień
    vehicle_data["inspection_notified_for_date"] = None
    vehicle_data["insurance_notified_for_date"] = None

    result = await db.vehicles.insert_one(vehicle_data)
    return {"id": str(result.inserted_id)}


@app.get("/api/vehicles")
async def get_vehicles(credentials: HTTPBasicCredentials = Depends(security)):
    user = await authenticate_user(credentials.username, credentials.password)
    if not user: raise HTTPException(status_code=401, detail="Unauthorized")

    vehicles = []
    async for vehicle in db.vehicles.find({"assigned_user_id": user["_id"]}):
        vehicle["_id"] = str(vehicle["_id"])
        vehicle["assigned_user_id"] = str(vehicle["assigned_user_id"])
        vehicle["inspection_date"] = vehicle["inspection_date"].isoformat()
        vehicle["insurance_date"] = vehicle["insurance_date"].isoformat()
        vehicles.append(vehicle)
    return vehicles


@app.put("/api/vehicles/{vehicle_id}")
async def update_vehicle(vehicle_id: str, vehicle: VehicleCreate,
                         credentials: HTTPBasicCredentials = Depends(security)):
    user = await authenticate_user(credentials.username, credentials.password)
    if not user: raise HTTPException(status_code=401, detail="Unauthorized")

    # Pobierz obecny pojazd z bazy
    current_vehicle = await db.vehicles.find_one({"_id": ObjectId(vehicle_id)})
    if not current_vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")

    update_data = vehicle.dict()
    new_inspection_date = datetime.strptime(vehicle.inspection_date, "%Y-%m-%d")
    new_insurance_date = datetime.strptime(vehicle.insurance_date, "%Y-%m-%d")

    update_data["inspection_date"] = new_inspection_date
    update_data["insurance_date"] = new_insurance_date
    update_data["assigned_user_id"] = user["_id"]

    # --- ZMIANA TUTAJ: Resetowanie flag powiadomień przy zmianie daty ---
    if current_vehicle["inspection_date"] != new_inspection_date:
        update_data["inspection_notified_for_date"] = None
    if current_vehicle["insurance_date"] != new_insurance_date:
        update_data["insurance_notified_for_date"] = None

    result = await db.vehicles.update_one({"_id": ObjectId(vehicle_id)}, {"$set": update_data})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    return {"message": "Vehicle updated successfully"}


@app.delete("/api/vehicles/{vehicle_id}")
async def delete_vehicle(vehicle_id: str, credentials: HTTPBasicCredentials = Depends(security)):
    user = await authenticate_user(credentials.username, credentials.password)
    if not user: raise HTTPException(status_code=401, detail="Unauthorized")

    result = await db.vehicles.delete_one({"_id": ObjectId(vehicle_id)})
    if result.deleted_count == 1:
        return {"message": "Vehicle deleted successfully"}
    raise HTTPException(status_code=404, detail="Vehicle not found")


@app.post("/api/vehicles/{vehicle_id}/notify")
async def send_vehicle_notification(vehicle_id: str, credentials: HTTPBasicCredentials = Depends(security)):
    user = await authenticate_user(credentials.username, credentials.password)
    if not user: raise HTTPException(status_code=401, detail="Unauthorized")

    vehicle = await db.vehicles.find_one({"_id": ObjectId(vehicle_id)})
    if not vehicle or not vehicle.get("email"):
        raise HTTPException(status_code=404, detail="Vehicle not found or email is missing")

    subject = f"Dane pojazdu: {vehicle['name']} ({vehicle['registration_number']})"
    html_body = f"""
    <html><body>
        <h2>Szczegóły pojazdu</h2>
        <p><b>Nazwa:</b> {vehicle.get('name', 'N/A')}</p>
        <p><b>Numer rejestracyjny:</b> {vehicle.get('registration_number', 'N/A')}</p>
        <p><b>VIN:</b> {vehicle.get('vin', 'N/A')}</p>
        <p><b>Data przeglądu:</b> {format_date_ymd(vehicle.get('inspection_date'))}</p>
        <p><b>Data ubezpieczenia:</b> {format_date_ymd(vehicle.get('insurance_date'))}</p>
        <p><b>Notatki:</b> {vehicle.get('notes', 'Brak')}</p>
    </body></html>
    """
    try:
        await send_email_async(subject, html_body, vehicle["email"], "flota@bestem.pl")
        return {"message": "Email sent successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send email: {e}")


@app.get("/", response_class=HTMLResponse)
async def read_root():
    with open("user.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)