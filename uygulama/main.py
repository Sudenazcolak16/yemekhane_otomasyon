import os
import bcrypt
import pymysql
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from typing import Optional, List

app = FastAPI(title="Yemekhane Otomasyonu API")

DB_WRITE_HOST = os.getenv("DB_YAZMA_HOST", "mysql-primary")
DB_READ_HOST = os.getenv("DB_OKUMA_HOST", "mysql-replica")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_NAME = os.getenv("DB_ADI", "yemekhane")
DB_USER = os.getenv("DB_KULLANICI", "yemekhane_app")
DB_PASS = os.getenv("DB_SIFRE", "Uyg_2026")


def get_conn(write: bool = False):
    host = DB_WRITE_HOST if write else DB_READ_HOST
    return pymysql.connect(
        host=host,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASS,
        database=DB_NAME,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def get_current_user(x_user_id: Optional[str] = Header(None)):
    if not x_user_id:
        raise HTTPException(status_code=401, detail="Oturum bulunamadı.")
    conn = get_conn(write=False)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, ad, soyad, eposta, rol, ogrenci_no, bakiye FROM kullanicilar WHERE id=%s AND aktif=1", (x_user_id,))
            user = cur.fetchone()
            if not user:
                raise HTTPException(status_code=401, detail="Geçersiz kullanıcı.")
            user["bakiye"] = float(user["bakiye"])
            return user
    finally:
        conn.close()


class LoginReq(BaseModel):
    eposta: str
    sifre: str


class RezReq(BaseModel):
    menu_id: int


class BakiyeReq(BaseModel):
    tutar: float


class DegerlendirmeReq(BaseModel):
    menu_id: int
    puan: int
    yorum: Optional[str] = ""


class TurnikeReq(BaseModel):
    kullanici_id: int
    yemekhane_id: int
    zaman: Optional[str] = None


class MenuEkleReq(BaseModel):
    yemekhane_id: int
    tarih: str
    ogun_id: int
    ogrenci_fiyati: float
    personel_fiyati: float
    yemek_idler: List[int]


@app.get("/api/saglik")
def saglik():
    return {"durum": "ok"}


@app.post("/api/giris")
def giris(req: LoginReq):
    conn = get_conn(write=False)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM kullanicilar WHERE eposta=%s AND aktif=1", (req.eposta.strip(),))
            user = cur.fetchone()
            if not user:
                raise HTTPException(status_code=401, detail="E-posta veya şifre hatalı.")
            hashed = user["sifre_hash"].encode("utf-8")
            if not bcrypt.checkpw(req.sifre.encode("utf-8"), hashed):
                raise HTTPException(status_code=401, detail="E-posta veya şifre hatalı.")
            return {
                "id": user["id"],
                "ad": user["ad"],
                "soyad": user["soyad"],
                "eposta": user["eposta"],
                "rol": user["rol"],
                "ogrenci_no": user["ogrenci_no"],
                "bakiye": float(user["bakiye"]),
            }
    finally:
        conn.close()


@app.get("/api/profil")
def profil(x_user_id: Optional[str] = Header(None)):
    return get_current_user(x_user_id)


@app.get("/api/menuler")
def menuler(tarih: Optional[str] = None, yemekhane_id: Optional[int] = None):
    conn = get_conn(write=False)
    try:
        with conn.cursor() as cur:
            sql = """
                SELECT m.id, m.tarih, m.ogrenci_fiyati, m.personel_fiyati,
                       y.id AS yemekhane_id, y.ad AS yemekhane_ad, y.kapasite,
                       o.id AS ogun_id, o.ad AS ogun_ad, o.servis_baslangic, o.servis_bitis, o.son_saat,
                       (SELECT COUNT(*) FROM rezervasyonlar r WHERE r.menu_id = m.id AND r.durum IN ('aktif','kullanildi')) AS dolu_sayisi
                FROM menuler m
                JOIN yemekhaneler y ON y.id = m.yemekhane_id
                JOIN ogunler o ON o.id = m.ogun_id
                WHERE 1=1
            """
            params = []
            if tarih:
                sql += " AND m.tarih = %s"
                params.append(tarih)
            else:
                sql += " AND m.tarih >= CURDATE()"
            if yemekhane_id:
                sql += " AND m.yemekhane_id = %s"
                params.append(yemekhane_id)
            sql += " ORDER BY m.tarih ASC, o.sira ASC, y.id ASC"
            cur.execute(sql, params)
            rows = cur.fetchall()
            for r in rows:
                r["tarih"] = str(r["tarih"])
                r["servis_baslangic"] = str(r["servis_baslangic"])
                r["servis_bitis"] = str(r["servis_bitis"])
                r["son_saat"] = str(r["son_saat"])
                r["ogrenci_fiyati"] = float(r["ogrenci_fiyati"])
                r["personel_fiyati"] = float(r["personel_fiyati"])
                cur.execute("""
                    SELECT ym.id, ym.ad, ym.kalori, ym.vejetaryen, k.ad AS kategori
                    FROM menu_yemekleri my
                    JOIN yemekler ym ON ym.id = my.yemek_id
                    JOIN yemek_kategorileri k ON k.id = ym.kategori_id
                    WHERE my.menu_id = %s
                    ORDER BY k.sira ASC
                """, (r["id"],))
                r["yemekler"] = cur.fetchall()
            return rows
    finally:
        conn.close()


@app.get("/api/rezervasyonlar")
def rezervasyonlar(x_user_id: Optional[str] = Header(None)):
    user = get_current_user(x_user_id)
    conn = get_conn(write=False)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT r.id, r.menu_id, r.durum, r.ucret, r.olusturma, r.kullanma_zamani, r.iptal_zamani,
                       m.tarih, y.ad AS yemekhane_ad, o.ad AS ogun_ad,
                       (SELECT puan FROM degerlendirmeler d WHERE d.kullanici_id = r.kullanici_id AND d.menu_id = r.menu_id) AS puan
                FROM rezervasyonlar r
                JOIN menuler m ON m.id = r.menu_id
                JOIN yemekhaneler y ON y.id = m.yemekhane_id
                JOIN ogunler o ON o.id = m.ogun_id
                WHERE r.kullanici_id = %s
                ORDER BY m.tarih DESC, r.id DESC
            """, (user["id"],))
            rows = cur.fetchall()
            for r in rows:
                r["tarih"] = str(r["tarih"])
                r["ucret"] = float(r["ucret"])
                r["olusturma"] = str(r["olusturma"])
                r["kullanma_zamani"] = str(r["kullanma_zamani"]) if r["kullanma_zamani"] else None
                r["iptal_zamani"] = str(r["iptal_zamani"]) if r["iptal_zamani"] else None
            return rows
    finally:
        conn.close()


@app.post("/api/rezervasyonlar")
def rezervasyon_yap(req: RezReq, x_user_id: Optional[str] = Header(None)):
    user = get_current_user(x_user_id)
    conn = get_conn(write=True)
    try:
        with conn.cursor() as cur:
            cur.callproc("sp_rezervasyon_yap", (user["id"], req.menu_id))
            res = cur.fetchone()
            return {"mesaj": "Rezervasyon oluşturuldu.", "sonuc": res}
    except pymysql.MySQLError as e:
        raise HTTPException(status_code=400, detail=e.args[1] if len(e.args) > 1 else str(e))
    finally:
        conn.close()


@app.post("/api/rezervasyonlar/{rez_id}/iptal")
def rezervasyon_iptal(rez_id: int, x_user_id: Optional[str] = Header(None)):
    user = get_current_user(x_user_id)
    conn = get_conn(write=True)
    try:
        with conn.cursor() as cur:
            cur.callproc("sp_rezervasyon_iptal", (user["id"], rez_id))
            res = cur.fetchone()
            return {"mesaj": "Rezervasyon iptal edildi.", "sonuc": res}
    except pymysql.MySQLError as e:
        raise HTTPException(status_code=400, detail=e.args[1] if len(e.args) > 1 else str(e))
    finally:
        conn.close()


@app.get("/api/bakiye")
def bakiye_gecmisi(x_user_id: Optional[str] = Header(None)):
    user = get_current_user(x_user_id)
    conn = get_conn(write=False)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, tur, tutar, rezervasyon_id, aciklama, olusturma
                FROM bakiye_hareketleri
                WHERE kullanici_id = %s
                ORDER BY id DESC LIMIT 50
            """, (user["id"],))
            rows = cur.fetchall()
            for r in rows:
                r["tutar"] = float(r["tutar"])
                r["olusturma"] = str(r["olusturma"])
            return {"bakiye": user["bakiye"], "hareketler": rows}
    finally:
        conn.close()


@app.post("/api/bakiye/yukle")
def bakiye_yukle(req: BakiyeReq, x_user_id: Optional[str] = Header(None)):
    user = get_current_user(x_user_id)
    conn = get_conn(write=True)
    try:
        with conn.cursor() as cur:
            cur.callproc("sp_bakiye_yukle", (user["id"], req.tutar))
            res = cur.fetchone()
            return {"mesaj": "Bakiye yüklendi.", "sonuc": res}
    except pymysql.MySQLError as e:
        raise HTTPException(status_code=400, detail=e.args[1] if len(e.args) > 1 else str(e))
    finally:
        conn.close()


@app.post("/api/degerlendirmeler")
def degerlendir(req: DegerlendirmeReq, x_user_id: Optional[str] = Header(None)):
    user = get_current_user(x_user_id)
    conn = get_conn(write=True)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id FROM rezervasyonlar
                WHERE kullanici_id=%s AND menu_id=%s AND durum='kullanildi'
            """, (user["id"], req.menu_id))
            if not cur.fetchone():
                raise HTTPException(status_code=400, detail="Yalnızca kullandığınız menüleri değerlendirebilirsiniz.")
            cur.execute("""
                INSERT INTO degerlendirmeler (kullanici_id, menu_id, puan, yorum)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE puan=VALUES(puan), yorum=VALUES(yorum)
            """, (user["id"], req.menu_id, req.puan, req.yorum))
            return {"mesaj": "Değerlendirmeniz kaydedildi."}
    finally:
        conn.close()


@app.post("/api/turnike")
def turnike(req: TurnikeReq):
    conn = get_conn(write=True)
    try:
        with conn.cursor() as cur:
            cur.callproc("sp_turnike_gecis", (req.kullanici_id, req.yemekhane_id, req.zaman))
            res = cur.fetchone()
            return {"mesaj": "Geçiş onaylandı.", "sonuc": res}
    except pymysql.MySQLError as e:
        raise HTTPException(status_code=400, detail=e.args[1] if len(e.args) > 1 else str(e))
    finally:
        conn.close()


@app.get("/api/admin/ozet")
def admin_ozet(x_user_id: Optional[str] = Header(None)):
    user = get_current_user(x_user_id)
    if user["rol"] != "yonetici":
        raise HTTPException(status_code=403, detail="Yetkisiz erişim.")
    conn = get_conn(write=False)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, ad FROM yemekhaneler")
            yemekhaneler = cur.fetchall()
            cur.execute("SELECT id, ad FROM ogunler ORDER BY sira")
            ogunler = cur.fetchall()
            cur.execute("SELECT y.id, y.ad, y.kalori, k.ad AS kategori FROM yemekler y JOIN yemek_kategorileri k ON k.id=y.kategori_id ORDER BY k.sira, y.ad")
            yemekler = cur.fetchall()
            cur.execute("SELECT id, ad, soyad, eposta, rol, bakiye FROM kullanicilar ORDER BY id LIMIT 30")
            kullanicilar = cur.fetchall()
            for k in kullanicilar:
                k["bakiye"] = float(k["bakiye"])
            cur.execute("""
                SELECT m.id, m.tarih, y.ad AS yemekhane, o.ad AS ogun,
                       COUNT(r.id) AS toplam_rez,
                       SUM(CASE WHEN r.durum='kullanildi' THEN 1 ELSE 0 END) AS kullanilan,
                       ROUND(AVG(d.puan), 2) AS ort_puan
                FROM menuler m
                JOIN yemekhaneler y ON y.id=m.yemekhane_id
                JOIN ogunler o ON o.id=m.ogun_id
                LEFT JOIN rezervasyonlar r ON r.menu_id=m.id AND r.durum IN ('aktif','kullanildi')
                LEFT JOIN degerlendirmeler d ON d.menu_id=m.id
                GROUP BY m.id
                ORDER BY m.tarih DESC LIMIT 20
            """)
            rapor = cur.fetchall()
            for rp in rapor:
                rp["tarih"] = str(rp["tarih"])
                rp["kullanilan"] = int(rp["kullanilan"] or 0)
                rp["ort_puan"] = float(rp["ort_puan"]) if rp["ort_puan"] else None
            return {
                "yemekhaneler": yemekhaneler,
                "ogunler": ogunler,
                "yemekler": yemekler,
                "kullanicilar": kullanicilar,
                "rapor": rapor,
            }
    finally:
        conn.close()


@app.post("/api/admin/menuler")
def admin_menu_ekle(req: MenuEkleReq, x_user_id: Optional[str] = Header(None)):
    user = get_current_user(x_user_id)
    if user["rol"] != "yonetici":
        raise HTTPException(status_code=403, detail="Yetkisiz erişim.")
    conn = get_conn(write=True)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO menuler (yemekhane_id, tarih, ogun_id, ogrenci_fiyati, personel_fiyati)
                VALUES (%s, %s, %s, %s, %s)
            """, (req.yemekhane_id, req.tarih, req.ogun_id, req.ogrenci_fiyati, req.personel_fiyati))
            menu_id = cur.lastrowid
            for yid in req.yemek_idler:
                cur.execute("INSERT INTO menu_yemekleri (menu_id, yemek_id) VALUES (%s, %s)", (menu_id, yid))
            return {"mesaj": "Yeni menü eklendi.", "menu_id": menu_id}
    except pymysql.MySQLError as e:
        raise HTTPException(status_code=400, detail=e.args[1] if len(e.args) > 1 else str(e))
    finally:
        conn.close()