from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    username: str
    password: str
    portal: str = "user"  # user | admin


class RegisterIn(BaseModel):
    sponsor_username: str
    package_id: int
    username: str | None = None
    first_name: str = Field(min_length=1)
    last_name: str = ""
    email: str = ""
    phone: str = ""
    gender: str = ""
    date_of_birth: str = ""
    country: str = "India"
    state: str = ""
    city: str = ""
    address: str = ""
    pincode: str = ""
    password: str
    txn_password: str | None = None
    payment_method: str = "pending"  # ewallet | pin | pending (members) ; admin registrations are always activated
    txn_password_confirm: str | None = None
    pin_code: str | None = None  # with payment_method "pin"; the pin decides the package


class ProfileIn(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    gender: str | None = None
    date_of_birth: str | None = None
    country: str | None = None
    state: str | None = None
    city: str | None = None
    address: str | None = None
    pincode: str | None = None
    bank_name: str | None = None
    account_holder: str | None = None
    account_number: str | None = None
    ifsc: str | None = None


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=6)


class MailIn(BaseModel):
    to_username: str
    subject: str = Field(min_length=1)
    body: str = Field(min_length=1)


class TransferIn(BaseModel):
    to_username: str
    amount: float = Field(gt=0)
    note: str = ""
    txn_password: str


class PayoutRequestIn(BaseModel):
    amount: float = Field(gt=0)
    txn_password: str
    method: str = "Bank Transfer"


class ActivateIn(BaseModel):
    payment_method: str  # pin | ewallet
    pin_code: str = ""
    txn_password: str = ""


class PinBuyIn(BaseModel):
    package_id: int
    quantity: int = Field(ge=1)
    txn_password: str


class PinTransferIn(BaseModel):
    to_username: str
    package_id: int
    quantity: int = Field(ge=1)
    txn_password: str


class FundIn(BaseModel):
    username: str
    type: str  # credit | debit
    amount: float = Field(gt=0)
    note: str = ""
    txn_password: str


class ActionIn(BaseModel):
    action: str
    note: str = ""


class PackageIn(BaseModel):
    name: str
    code: str
    price: float = Field(ge=0)
    pv: int = Field(ge=0)
    validity_days: int = 365
    description: str = ""
    product_name: str = ""
    product_amount: float = Field(default=0, ge=0)
    commission_amount: float = Field(default=0, ge=0)
    franchise_commission: float = Field(default=0, ge=0)
    gst_percent: float = Field(default=0, ge=0, le=100)
    is_active: bool = True


class NewsIn(BaseModel):
    title: str
    body: str


class PlanLevelIn(BaseModel):
    level: int
    amount: float = Field(ge=0)
    reward_amount: float = Field(default=0, ge=0)
    autopool_amount: float = Field(default=0, ge=0)
    reward_name: str = ""


class SettingsIn(BaseModel):
    values: dict[str, str]


class AdminPasswordResetIn(BaseModel):
    new_password: str = Field(min_length=6)
    kind: str = "login"  # login | transaction
