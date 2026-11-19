import os
import json
import uuid
import logging
from datetime import datetime, date
import bcrypt
import pymysql
import redis
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from typing import Optional, List

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("yemekhane")

app = FastAPI(title="Yemekhane Otomasyonu API")

DB_WRITE_HOST = os.getenv("DB_YAZMA_HOST", "mysql-primary")
DB_READ_HOST = os.getenv("DB_OKUMA_HOST", "mysql-replica")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_NAME = os.getenv("DB_ADI", "yemekhane")
DB_USER = os.getenv("DB_KULLANICI", "yemekhane_app")
DB_PASS = os.getenv("DB_SIFRE", "Uyg_2026")

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
OTURUM_SURESI = 1800  # 30 dakika

rdb = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)


def gun_sonuna_kalan_saniye() -> int:
    simdi = datetime.now()
    gun_sonu = datetime.combine(simdi.date(), datetime.max.time()).replace(microsecond=0)
    kalan = int((gun_sonu - simdi).total_seconds())
    return max(kalan, 60)


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


def get_current_user(
    authorization: Optional[str] = Header(None),
    x_user_id: Optional[str] = Header(None),
):
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.replace("Bearer ", "").strip()

    if token:
        oturum_anahtari = f"oturum:{token}"
        oturum_verisi = rdb.get(oturum_anahtari)
        if not oturum_verisi:
            raise HTTPException(status_code=401, detail="Oturum süresi dolmuş veya geçersiz.")
        # Her istekte oturum suresini 30 dakikaya yenile
        rdb.expire(oturum_anahtari, OTURUM_SURESI)
        kullanici = json.loads(oturum_verisi)
        # Guncel bakiye kontrolu (replikasyon gecikmesi olmamasi icin gerektiginde DB'den tazele)
        conn = get_conn(write=True)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT bakiye FROM kullanicilar WHERE id=%s AND aktif=1", (kullanici["id"],))
                row = cur.fetchone()
                if row:
                    kullanici["bakiye"] = float(row["bakiye"])
        finally:
            conn.close()
        return kullanici

    # Geriye donuk uyumluluk (eski istemci istekleri icin)
    if x_user_id:
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

    raise HTTPException(status_code=401, detail="Oturum bulunamadı.")


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
    return {"durum": "ok", "redis": rdb.ping()}


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

            kullanici_bilgi = {
                "id": user["id"],
                "ad": user["ad"],
                "soyad": user["soyad"],
                "eposta": user["eposta"],
                "rol": user["rol"],
                "ogrenci_no": user["ogrenci_no"],
                "bakiye": float(user["bakiye"]),
            }
            token = str(uuid.uuid4())
            oturum_anahtari = f"oturum:{token}"
            rdb.setex(oturum_anahtari, OTURUM_SURESI, json.dumps(kullanici_bilgi, ensure_ascii=False))
            logger.info(f"[OTURUM OLUSTURULDU] {oturum_anahtari} (TTL: {OTURUM_SURESI}s)")

            return {**kullanici_bilgi, "token": token}
    finally:
        conn.close()


@app.post("/api/cikis")
def cikis(authorization: Optional[str] = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        token = authorization.replace("Bearer ", "").strip()
        oturum_anahtari = f"oturum:{token}"
        rdb.delete(oturum_anahtari)
        logger.info(f"[OTURUM SILINDI] {oturum_anahtari}")
    return {"mesaj": "Çıkış yapıldı."}


@app.get("/api/profil")
def profil(authorization: Optional[str] = Header(None), x_user_id: Optional[str] = Header(None)):
    return get_current_user(authorization, x_user_id)


def _tek_menu_getir_veya_onbellekle(cur, yemekhane_id: int, tarih_str: str, ogun_id: int):
    cache_key = f"menu:{yemekhane_id}:{tarih_str}:{ogun_id}"
    cached = rdb.get(cache_key)
    if cached:
        logger.info(f"[CACHE HIT] Redis'ten okundu -> {cache_key}")
        menu_obj = json.loads(cached)
        # Canli doluluk sayisini replikadan guncelle
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM rezervasyonlar WHERE menu_id = %s AND durum IN ('aktif','kullanildi')",
            (menu_obj["id"],),
        )
        menu_obj["dolu_sayisi"] = cur.fetchone()["cnt"]
        return menu_obj

    logger.info(f"[CACHE MISS] Veritabanindan okunuyor -> {cache_key}")
    cur.execute(
        """
        SELECT m.id, m.tarih, m.ogrenci_fiyati, m.personel_fiyati,
               y.id AS yemekhane_id, y.ad AS yemekhane_ad, y.kapasite,
               o.id AS ogun_id, o.ad AS ogun_ad, o.servis_baslangic, o.servis_bitis, o.son_saat,
               (SELECT COUNT(*) FROM rezervasyonlar r WHERE r.menu_id = m.id AND r.durum IN ('aktif','kullanildi')) AS dolu_sayisi
        FROM menuler m
        JOIN yemekhaneler y ON y.id = m.yemekhane_id
        JOIN ogunler o ON o.id = m.ogun_id
        WHERE m.yemekhane_id = %s AND m.tarih = %s AND m.ogun_id = %s
        """,
        (yemekhane_id, tarih_str, ogun_id),
    )
    row = cur.fetchone()
    if not row:
        return None

    row["tarih"] = str(row["tarih"])
    row["servis_baslangic"] = str(row["servis_baslangic"])
    row["servis_bitis"] = str(row["servis_bitis"])
    row["son_saat"] = str(row["son_saat"])
    row["ogrenci_fiyati"] = float(row["ogrenci_fiyati"])
    row["personel_fiyati"] = float(row["personel_fiyati"])
    cur.execute(
        """
        SELECT ym.id, ym.ad, ym.kalori, ym.vejetaryen, k.ad AS kategori
        FROM menu_yemekleri my
        JOIN yemekler ym ON ym.id = my.yemek_id
        JOIN yemek_kategorileri k ON k.id = ym.kategori_id
        WHERE my.menu_id = %s
        ORDER BY k.sira ASC
        """,
        (row["id"],),
    )
    row["yemekler"] = cur.fetchall()

    ttl = gun_sonuna_kalan_saniye()
    rdb.setex(cache_key, ttl, json.dumps(row, ensure_ascii=False))
    logger.info(f"[CACHE SET] Redis'e yazildi -> {cache_key} (TTL: {ttl}s)")
    return row


@app.get("/api/menuler")
def menuler(
    tarih: Optional[str] = None,
    yemekhane_id: Optional[int] = None,
    ogun_id: Optional[int] = None,
):
    conn = get_conn(write=False)
    try:
        with conn.cursor() as cur:
            # Belirli bir yemekhane + tarih + ogun sorgulandiginda dogrudan anahtardan getir
            if yemekhane_id and tarih and ogun_id:
                tek = _tek_menu_getir_veya_onbellekle(cur, yemekhane_id, tarih, ogun_id)
                return [tek] if tek else []

            sql = """
                SELECT m.yemekhane_id, m.tarih, m.ogun_id
                FROM menuler m
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
            if ogun_id:
                sql += " AND m.ogun_id = %s"
                params.append(ogun_id)
            sql += " ORDER BY m.tarih ASC, o.sira ASC, m.yemekhane_id ASC"
            cur.execute(sql, params)
            hedefler = cur.fetchall()

            sonuclar = []
            for h in hedefler:
                item = _tek_menu_getir_veya_onbellekle(
                    cur, h["yemekhane_id"], str(h["tarih"]), h["ogun_id"]
                )
                if item:
                    sonuclar.append(item)
            return sonuclar
    finally:
        conn.close()


@app.get("/api/rezervasyonlar")
def rezervasyonlar(authorization: Optional[str] = Header(None), x_user_id: Optional[str] = Header(None)):
    user = get_current_user(authorization, x_user_id)
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
def rezervasyon_yap(req: RezReq, authorization: Optional[str] = Header(None), x_user_id: Optional[str] = Header(None)):
    user = get_current_user(authorization, x_user_id)
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
def rezervasyon_iptal(rez_id: int, authorization: Optional[str] = Header(None), x_user_id: Optional[str] = Header(None)):
    user = get_current_user(authorization, x_user_id)
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
def bakiye_gecmisi(authorization: Optional[str] = Header(None), x_user_id: Optional[str] = Header(None)):
    user = get_current_user(authorization, x_user_id)
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
def bakiye_yukle(req: BakiyeReq, authorization: Optional[str] = Header(None), x_user_id: Optional[str] = Header(None)):
    user = get_current_user(authorization, x_user_id)
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
def degerlendir(req: DegerlendirmeReq, authorization: Optional[str] = Header(None), x_user_id: Optional[str] = Header(None)):
    user = get_current_user(authorization, x_user_id)
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
def admin_ozet(authorization: Optional[str] = Header(None), x_user_id: Optional[str] = Header(None)):
    user = get_current_user(authorization, x_user_id)
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
def admin_menu_ekle_veya_guncelle(req: MenuEkleReq, authorization: Optional[str] = Header(None), x_user_id: Optional[str] = Header(None)):
    user = get_current_user(authorization, x_user_id)
    if user["rol"] != "yonetici":
        raise HTTPException(status_code=403, detail="Yetkisiz erişim.")
    conn = get_conn(write=True)
    try:
        with conn.cursor() as cur:
            # Ayni yemekhane + tarih + ogun icin menu varsa guncelle, yoksa ekle
            cur.execute(
                "SELECT id FROM menuler WHERE yemekhane_id=%s AND tarih=%s AND ogun_id=%s",
                (req.yemekhane_id, req.tarih, req.ogun_id),
            )
            mevcut = cur.fetchone()
            if mevcut:
                menu_id = mevcut["id"]
                cur.execute(
                    "UPDATE menuler SET ogrenci_fiyati=%s, personel_fiyati=%s WHERE id=%s",
                    (req.ogrenci_fiyati, req.personel_fiyati, menu_id),
                )
                cur.execute("DELETE FROM menu_yemekleri WHERE menu_id=%s", (menu_id,))
                mesaj = "Mevcut menü güncellendi."
            else:
                cur.execute(
                    """
                    INSERT INTO menuler (yemekhane_id, tarih, ogun_id, ogrenci_fiyati, personel_fiyati)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (req.yemekhane_id, req.tarih, req.ogun_id, req.ogrenci_fiyati, req.personel_fiyati),
                )
                menu_id = cur.lastrowid
                mesaj = "Yeni menü eklendi."

            for yid in req.yemek_idler:
                cur.execute("INSERT INTO menu_yemekleri (menu_id, yemek_id) VALUES (%s, %s)", (menu_id, yid))

            # Redis onbellegini gecersiz kil
            cache_key = f"menu:{req.yemekhane_id}:{req.tarih}:{req.ogun_id}"
            silinen = rdb.delete(cache_key)
            logger.info(f"[CACHE INVALIDATE] {cache_key} silindi (silinen={silinen})")

            return {"mesaj": mesaj, "menu_id": menu_id}
    except pymysql.MySQLError as e:
        raise HTTPException(status_code=400, detail=e.args[1] if len(e.args) > 1 else str(e))
    finally:
        conn.close()