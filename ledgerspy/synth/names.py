"""Fictional identities. Nothing here refers to a real person, company, address, phone or account.

* Company names combine invented word stems with ordinary trade words, so they read naturally but are made up.
* Cities are invented. Phones use "+91-0…": a leading 0 after the country code is never an allocated Indian
  number. E-mail domains end in `.example` (reserved by RFC 2606).
* Bank accounts ("SYN…"), bank codes ("SYNB0…") and tax IDs ("SYN-TX-…") use formats that cannot collide with
  real PAN/GSTIN/IFSC/account numbers.
* Person names are common given/family names combined at random; they identify no one.
"""

import random

STEMS = [
    "Varnel",
    "Quillon",
    "Drevik",
    "Solvane",
    "Teravin",
    "Mirelle",
    "Ostrel",
    "Calvane",
    "Brennick",
    "Halvorn",
    "Pemberline",
    "Corvane",
    "Mardale",
    "Ilvaran",
    "Kesrin",
    "Lumora",
    "Navari",
    "Orlesh",
    "Prevane",
    "Ruskel",
    "Saverin",
    "Tolvane",
    "Ulmora",
    "Vantrel",
    "Wexley",
    "Zorvane",
    "Ambrel",
    "Belvora",
    "Castrel",
    "Delvane",
    "Eskarel",
    "Fenmora",
    "Galvine",
    "Harrowin",
    "Isolde",
    "Javrel",
    "Kelmora",
    "Larvane",
    "Merivel",
    "Norvane",
    "Ombrel",
    "Parvel",
    "Quorven",
    "Rhendel",
    "Sorvane",
    "Trelane",
    "Ulvane",
    "Verrin",
    "Wyndel",
    "Yaravel",
    "Zentrel",
    "Arkholm",
    "Brightwell",
    "Celdon",
    "Dunmore",
    "Elvaran",
    "Falcrest",
    "Glenmora",
    "Hessarin",
]

TRADE = {
    "raw_materials": ["Metals", "Alloys", "Steel Traders", "Polymers", "Castings", "Industrial Supplies"],
    "packaging": ["Packaging", "Packing Solutions", "Cartons", "Containers", "Wraps"],
    "logistics": ["Logistics", "Freight Carriers", "Transport", "Cargo Movers", "Roadways"],
    "utilities": ["Power Services", "Energy", "Water Works", "Utility Services"],
    "it_services": ["Infotech", "Systems", "Software Services", "Digital Solutions", "Networks"],
    "facilities": ["Facility Services", "Housekeeping", "Property Care", "Estates Services"],
    "office_supplies": ["Stationers", "Office Mart", "Office Supplies", "Paper Co"],
    "professional": ["Advisors", "Consultants", "Associates", "Legal Partners", "Audit Partners"],
    "staffing": ["Staffing", "Manpower Services", "HR Solutions", "Workforce"],
    "maintenance": ["Engineering Works", "Maintenance Services", "Electricals", "Tooling"],
    "trading": ["Trade Links", "Trading Co", "Exim Traders", "Commercial Agencies"],
    "customer": ["Industries", "Manufacturing", "Automotive", "Appliances", "Enterprises", "Engineering"],
}
SUFFIX = ["Pvt Ltd", "Private Limited", "LLP", "Pvt Ltd", "& Co", "Pvt Ltd"]

FIRST = [
    "Aarav",
    "Aditi",
    "Akash",
    "Ananya",
    "Arjun",
    "Bhavna",
    "Chetan",
    "Deepa",
    "Devika",
    "Farhan",
    "Gaurav",
    "Isha",
    "Jatin",
    "Kavya",
    "Kiran",
    "Lakshmi",
    "Manav",
    "Meera",
    "Nikhil",
    "Nisha",
    "Pooja",
    "Pranav",
    "Rahul",
    "Riya",
    "Rohan",
    "Sanjana",
    "Sahil",
    "Sneha",
    "Tanvi",
    "Uday",
    "Varun",
    "Vidya",
    "Yash",
    "Zoya",
    "Harsh",
    "Ira",
    "Kabir",
    "Lavanya",
    "Mohit",
    "Neha",
    "Omkar",
    "Pallavi",
    "Ritesh",
    "Shreya",
    "Tarun",
    "Ujwala",
]
LAST = [
    "Acharya",
    "Bhat",
    "Chandra",
    "Desai",
    "Fernandes",
    "Gupta",
    "Iyer",
    "Joshi",
    "Kamath",
    "Kulkarni",
    "Menon",
    "Mehta",
    "Nair",
    "Pai",
    "Patil",
    "Rao",
    "Reddy",
    "Saxena",
    "Shetty",
    "Sinha",
    "Thakur",
    "Varma",
    "Verma",
]

CITIES = [
    ("Veltara", "MH"),
    ("Navelpur", "MH"),
    ("Korhal", "GJ"),
    ("Tamrisa", "KA"),
    ("Delvapur", "KA"),
    ("Orvana", "TN"),
    ("Kesrigarh", "RJ"),
    ("Sunalpet", "TS"),
    ("Ishvani Nagar", "MP"),
    ("Morvik", "WB"),
]
REMOTE_CITY = ("Morvik", "WB")  # reserved for S-X-01: nobody else is ever placed there
ESTATES = ["Navrang", "Sahyadri", "Kanakpura", "Riverside", "Greenfield", "Eastgate", "Lotus", "Silverline"]
ROADS = ["Station", "Market", "Temple", "Lake", "Mill", "Canal", "Hill", "College"]
BUILDINGS = ["Arcade", "Plaza", "Towers", "Chambers", "Complex", "House"]


LEGAL_WORDS = {"Pvt", "Ltd", "LLP", "Private", "Limited", "Co", "&"}


def core(name: str) -> str:
    """A firm name without its legal form: "Navari Cartons Pvt Ltd" and "Navari Cartons LLP" share a core."""
    words = [w for w in name.split() if w.strip(".") not in LEGAL_WORDS]
    return "".join(ch for ch in "".join(words).lower() if ch.isalnum())


class NameBook:
    """Hands out unique fictional names and identifiers from a seeded stream."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.used: set[str] = set()
        self.cores: set[str] = set()  # firm names: no two firms differ only in their legal form

    def take(self, firm: str) -> str:
        self.used.add(firm)
        self.cores.add(core(firm))
        return firm

    def _unique(self, make) -> str:
        for _ in range(10_000):
            value = make()
            if value not in self.used:
                self.used.add(value)
                return value
        raise RuntimeError("name space exhausted")  # pragma: no cover

    def company(self, category: str) -> str:
        r = self.rng
        for _ in range(10_000):
            name = f"{r.choice(STEMS)} {r.choice(TRADE[category])} {r.choice(SUFFIX)}"
            if core(name) not in self.cores:
                return self.take(name)
        raise RuntimeError("name space exhausted")  # pragma: no cover

    def person(self) -> str:
        r = self.rng
        return self._unique(lambda: f"{r.choice(FIRST)} {r.choice(LAST)}")

    def city(self) -> tuple[str, str]:
        return self.rng.choice([c for c in CITIES if c != REMOTE_CITY])

    def business_address(self, city: tuple[str, str] | None = None) -> str:
        r = self.rng
        name, state = city or self.city()
        return self._unique(
            lambda: (
                f"Plot {r.randint(1, 240)}, {r.choice(ESTATES)} Industrial Estate, {name}, {state} "
                f"{r.randint(400, 799)}{r.randint(100, 999)}"
            )
        )

    def home_address(self, city: tuple[str, str] | None = None) -> str:
        r = self.rng
        name, state = city or self.city()
        return self._unique(
            lambda: (
                f"Flat {r.randint(1, 30)}0{r.randint(1, 9)}, {r.choice(ESTATES)} {r.choice(BUILDINGS)}, "
                f"{r.choice(ROADS)} Road, {name}, {state} {r.randint(400, 799)}{r.randint(100, 999)}"
            )
        )

    def phone(self) -> str:
        r = self.rng
        return self._unique(lambda: f"+91-0{r.randint(1000, 9999)}-{r.randint(10000, 99999)}")

    def bank_account(self) -> str:
        r = self.rng
        return self._unique(lambda: f"SYN{r.randint(10**11, 10**12 - 1)}")

    def bank_code(self) -> str:
        r = self.rng
        return f"SYNB0{r.randint(100000, 999999)}"

    def tax_id(self) -> str:
        r = self.rng
        return self._unique(
            lambda: f"SYN-TX-{r.randint(10**6, 10**7 - 1)}{r.choice('ABCDEFGHJKLMNPRSTVWXYZ')}"
        )

    def invoice_prefix(self, name: str) -> str:
        words = [w for w in name.replace("&", " ").split() if w[:1].isalpha()]
        base = "".join(w[0] for w in words[:3]).upper()
        return self._unique(lambda: base + str(self.rng.randint(2, 9)))

    def free_stem(self, preferred: list[str], *templates: str) -> str:
        """The first stem (preferred ones first, then every stem) for which no `template.format(stem)` name is
        taken yet: scenario entities with prescribed names never duplicate a generated vendor's name."""
        for stem in preferred + [x for x in STEMS if x not in preferred]:
            if all(core(t.format(stem)) not in self.cores for t in templates):
                return stem
        raise RuntimeError("no free stem")  # pragma: no cover

    @staticmethod
    def email(name: str, domain_from: str) -> str:
        """Local part from `name`; the domain is the core of `domain_from`, so two firms share an
        e-mail domain only if they share a name (a shared address/phone/account is always deliberate)."""
        local = ".".join(name.lower().split()[:2])
        return f"{local}@{core(domain_from)}.example"
