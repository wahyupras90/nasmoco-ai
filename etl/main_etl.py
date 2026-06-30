"""
main_etl.py
===========
Orchestrator ETL Nasmoco Tegal.
Jalankan semua modul ETL secara berurutan.

Cara pakai:
  python main_etl.py        -> jalankan semua
  python main_etl.py --daily   -> ETL harian
  python main_etl.py --monthly -> ETL bulanan (termasuk customer_profile, segment_rfm,
                                   marketing_program, crm_attack_list)
"""

import time
import sqlite3
from pathlib import Path
from etl_helpers import PATHS, DB_PATH

# Import semua modul ETL
from ETL_update_alamat import (
    load_masters, load_rs, load_invoice,
    load_unitmasuk, load_target_manual,
    enrich_unitmasuk, load_parts,
    build_rekapbulanan, save_to_db,
    build_daily_kpi, load_rs_full,
    get_excel_files, get_months_in_df,
    parse_satu_parts,
)
from etl_rs                import run as run_rs
from etl_tcare_unit        import run as run_tcare_unit, run_tcare_unit_daily, rebuild_tcare_monthly
from etl_master_adt        import run as run_master_adt
from etl_customer_profile  import run as run_customer_profile
from etl_segment_rfm       import run as run_segment_rfm
from etl_marketing_program import run as run_marketing_program
from etl_crm_attack_list   import create_crm_attack_list_table, run as run_crm_attack_list


def run_crm_pipeline():
    """
    Pipeline CRM: master_adt -> customer_profile -> segment_rfm
                  -> marketing_program -> crm_attack_list
    Urutan ini WAJIB dijaga karena tiap step bergantung pada step sebelumnya.
    """
    print('\n--- CRM Pipeline ---')
    print('a) master_adt...')
    run_master_adt()

    print('\nb) customer_profile...')
    run_customer_profile()

    print('\nc) segment_rfm...')
    run_segment_rfm()

    print('\nd) marketing_program...')
    run_marketing_program()

    print('\ne) crm_attack_list...')
    create_crm_attack_list_table()
    run_crm_attack_list()


def run_all():
    t_start = time.time()
    print('\n' + '='*55)
    print('  ETL Nasmoco Tegal (Modular)')
    print('='*55)

    Path(PATHS['output']).mkdir(parents=True, exist_ok=True)
    print(f'   DB Path: {DB_PATH}')

    # -- 1. Master files --
    print('\n1/8  Load master...')
    oli_map, sublet_map = load_masters(PATHS['master'])
    print(f'     -> {len(oli_map)} oli, {len(sublet_map)} sublet')

    # -- 2. RS (untuk enrich unitmasuk) --
    print('2/8  Load RS (enrich)...')
    df_rs = load_rs(PATHS['rs'])
    print(f'     -> {len(df_rs):,} unit')

    # -- 3. Invoice --
    print('3/8  Load invoice...')
    df_inv = load_invoice(PATHS['invoice'])
    print(f'     -> {len(df_inv):,} WOs')

    # -- 4. Unit masuk --
    print('4/8  Load unit masuk...')
    df_um_raw = load_unitmasuk(PATHS['unit_masuk'])
    print(f'     -> {len(df_um_raw):,} baris, {df_um_raw["no_wo"].nunique():,} WOs')

    # -- 4b. Target --
    print('4b/8 Load target...')
    df_target = load_target_manual(PATHS['master'], df_um_raw)
    if len(df_target) > 0:
        print(f'     -> {len(df_target)} rows target')
    else:
        print('     [!] Target tidak tersedia')

    # -- 5. Enrich unitmasuk --
    print('5/8  Enrich unitmasuk...')
    df_um = enrich_unitmasuk(df_um_raw, sublet_map, df_inv, df_rs)
    own   = df_um['is_own'].sum()
    tcare = (df_um['tcare'] == 'TCARE').sum()
    print(f'     -> {len(df_um):,} baris (Own: {own:,}, TCARE: {tcare:,})')

    # -- 6. Parts --
    print('6/8  Load parts...')
    df_bp = load_parts(PATHS['parts_baru'], PATHS['parts_cache'], oli_map, df_um)
    print(f'     -> {len(df_bp):,} baris')

    # -- 7. Rekapbulanan --
    print('7/8  Build rekapbulanan...')
    df_rb = build_rekapbulanan(df_inv, df_bp, df_um)
    print(f'     -> {len(df_rb):,} baris')

    # RS full untuk DB (kolom lama, backward compat)
    print('\n   Load RS full (lama)...')
    # rs ditangani etl_rs.py

    # Deteksi baru_months
    baru_months = set()
    for f in get_excel_files(PATHS['parts_baru']):
        try:
            df_tmp = parse_satu_parts(f)
            if len(df_tmp) > 0:
                baru_months |= get_months_in_df(df_tmp, 'tgl_faktur')
        except Exception:
            pass

    # Simpan tabel utama
    save_to_db(DB_PATH, df_um, df_inv, df_bp, df_rb,
               df_target, baru_months)
    build_daily_kpi(DB_PATH)

    # -- 8. RS master unit + TCARE Unit (modul baru) --
    print('\n8/8  Load RS master + TCARE Unit...')
    run_rs()
    run_tcare_unit()

    # -- 9. CRM Pipeline (master_adt, customer_profile, segment_rfm,
    #       marketing_program, crm_attack_list) --
    run_crm_pipeline()

    elapsed = time.time() - t_start
    print(f'\nSelesai! Waktu ETL: {int(elapsed//60)}m {int(elapsed%60)}s')
    print('='*55 + '\n')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--daily',   action='store_true',
                        help='Hanya ETL harian (unitmasuk, parts, dll)')
    parser.add_argument('--monthly', action='store_true',
                        help='Hanya ETL bulanan (rs, tcare_unit, CRM pipeline)')
    args = parser.parse_args()

    if args.daily:
        print("\n> Mode: HARIAN")
        # ETL_update_alamat (unitmasuk, rekapbulanan, daily_kpi, bufferparts)
        from ETL_update_alamat import run as run_daily
        run_daily()
        # Update tcare_unit ringan (patch WO baru saja)
        run_tcare_unit_daily()
    elif args.monthly:
        print("\n> Mode: BULANAN")
        # Baca Mapping Cust RAW sekali, share ke kedua modul
        from etl_rs import load_mapping_cust_raw
        df_map_raw = load_mapping_cust_raw()
        run_rs(map_cache=df_map_raw)
        run_tcare_unit(map_cache=df_map_raw)

        # CRM pipeline (customer_profile butuh rs & tcare_unit yang sudah fresh)
        run_crm_pipeline()
    else:
        run_all()
