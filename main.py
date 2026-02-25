from decimal import Decimal
import os
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator
from supabase import create_client, Client


load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

app = FastAPI(title="Evolzy Internal Finance Tracker API")


cors_origins_env = os.environ.get("CORS_ORIGINS", "")
origins = [o.strip() for o in cors_origins_env.split(",") if o.strip()]

if not origins:
    origins = ["http://localhost:3000", "http://localhost:5173"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ProjectBase(BaseModel):
    project_name: str
    client_name: str
    client_type: str = Field(...)
    order_price: Decimal = Field(..., gt=0)
    editor_payment: Decimal = Field(..., ge=0)
    status: str = Field(...)

    @validator("client_type")
    def validate_client_type(cls, v: str) -> str:
        v = v.lower()
        if v not in ("direct", "fiverr"):
            raise ValueError("client_type must be 'direct' or 'fiverr'")
        return v

    @validator("status")
    def validate_status(cls, v: str) -> str:
        v = v.lower()
        if v not in ("paid", "pending"):
            raise ValueError("status must be 'paid' or 'pending'")
        return v


class ProjectCreate(ProjectBase):
    pass


class ProjectUpdate(BaseModel):
    order_price: Optional[Decimal] = Field(None, gt=0)
    editor_payment: Optional[Decimal] = Field(None, ge=0)
    status: Optional[str] = Field(None)

    @validator("status")
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.lower()
        if v not in ("paid", "pending"):
            raise ValueError("status must be 'paid' or 'pending'")
        return v


class ProjectInDB(ProjectBase):
    id: str
    platform_fee: Decimal
    gateway_fee: Decimal
    net_received: Decimal
    profit: Decimal
    created_at: Optional[str]


class SummaryResponse(BaseModel):
    total_projects: int
    total_revenue_received: Decimal
    total_editor_payments: Decimal
    total_profit: Decimal


def compute_financials(
    client_type: str,
    order_price: Decimal,
    editor_payment: Decimal,
) -> dict:
    client_type = client_type.lower()

    if client_type == "direct":
        platform_fee = Decimal("0")
        gateway_fee = Decimal("2")
        net_received = order_price - gateway_fee
    elif client_type == "fiverr":
        platform_fee = (order_price * Decimal("0.20")).quantize(Decimal("0.01"))
        gateway_fee = Decimal("3")
        net_received = order_price - platform_fee - gateway_fee
    else:
        raise ValueError("Invalid client_type")

    profit = net_received - editor_payment

    return {
        "platform_fee": platform_fee,
        "gateway_fee": gateway_fee,
        "net_received": net_received,
        "profit": profit,
    }


def row_to_project(row: dict) -> ProjectInDB:
    return ProjectInDB(
        id=row["id"],
        project_name=row["project_name"],
        client_name=row["client_name"],
        client_type=row["client_type"],
        order_price=Decimal(str(row["order_price"])),
        platform_fee=Decimal(str(row["platform_fee"])),
        gateway_fee=Decimal(str(row["gateway_fee"])),
        net_received=Decimal(str(row["net_received"])),
        editor_payment=Decimal(str(row["editor_payment"])),
        profit=Decimal(str(row["profit"])),
        status=row["status"],
        created_at=row.get("created_at"),
    )


def get_project_or_404(project_id: str) -> dict:
    resp = supabase.table("projects").select("*").eq("id", project_id).single().execute()
    if not resp.data:
        raise HTTPException(status_code=404, detail="Project not found")
    return resp.data


@app.post("/projects", response_model=ProjectInDB)
def create_project(payload: ProjectCreate):
    fees = compute_financials(
        client_type=payload.client_type,
        order_price=payload.order_price,
        editor_payment=payload.editor_payment,
    )

    data = {
        "project_name": payload.project_name,
        "client_name": payload.client_name,
        "client_type": payload.client_type,
        "order_price": str(payload.order_price),
        "platform_fee": str(fees["platform_fee"]),
        "gateway_fee": str(fees["gateway_fee"]),
        "net_received": str(fees["net_received"]),
        "editor_payment": str(payload.editor_payment),
        "profit": str(fees["profit"]),
        "status": payload.status,
    }

    resp = supabase.table("projects").insert(data).execute()
    if not resp.data:
        raise HTTPException(status_code=500, detail="Failed to create project")

    return row_to_project(resp.data[0])


@app.get("/projects", response_model=List[ProjectInDB])
def list_projects():
    resp = supabase.table("projects").select("*").order("created_at", desc=True).execute()
    rows = resp.data or []
    return [row_to_project(r) for r in rows]


@app.get("/projects/{project_id}", response_model=ProjectInDB)
def get_project(project_id: str):
    row = get_project_or_404(project_id)
    return row_to_project(row)


@app.put("/projects/{project_id}", response_model=ProjectInDB)
def update_project(project_id: str, payload: ProjectUpdate):
    existing = get_project_or_404(project_id)

    new_order_price = Decimal(str(payload.order_price or existing["order_price"]))
    new_editor_payment = Decimal(str(payload.editor_payment or existing["editor_payment"]))
    new_status = payload.status or existing["status"]

    fees = compute_financials(
        client_type=existing["client_type"],
        order_price=new_order_price,
        editor_payment=new_editor_payment,
    )

    updates = {
        "order_price": str(new_order_price),
        "editor_payment": str(new_editor_payment),
        "platform_fee": str(fees["platform_fee"]),
        "gateway_fee": str(fees["gateway_fee"]),
        "net_received": str(fees["net_received"]),
        "profit": str(fees["profit"]),
        "status": new_status,
    }

    resp = supabase.table("projects").update(updates).eq("id", project_id).execute()
    if not resp.data:
        raise HTTPException(status_code=500, detail="Failed to update project")

    return row_to_project(resp.data[0])


@app.delete("/projects/{project_id}")
def delete_project(project_id: str):
    get_project_or_404(project_id)
    supabase.table("projects").delete().eq("id", project_id).execute()
    return {"detail": "Project deleted"}


@app.get("/summary", response_model=SummaryResponse)
def get_summary():
    count_resp = supabase.table("projects").select("id", count="exact").execute()
    total_projects = count_resp.count or 0

    resp = supabase.table("projects").select(
        "net_received,editor_payment,profit"
    ).execute()
    rows = resp.data or []

    total_revenue_received = sum(Decimal(str(r["net_received"])) for r in rows) if rows else Decimal("0")
    total_editor_payments = sum(Decimal(str(r["editor_payment"])) for r in rows) if rows else Decimal("0")
    total_profit = sum(Decimal(str(r["profit"])) for r in rows) if rows else Decimal("0")

    return SummaryResponse(
        total_projects=total_projects,
        total_revenue_received=total_revenue_received,
        total_editor_payments=total_editor_payments,
        total_profit=total_profit,
    )

