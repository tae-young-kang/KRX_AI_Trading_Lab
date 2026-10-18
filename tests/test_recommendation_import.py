"""Local packages and temporary DB only; no source/network/order calls."""
from datetime import date
from copy import deepcopy
import json
import pytest
from app.recommendation_import import import_package,load_package
from test_recommendation_reports import store
from test_recommendation_outcomes import DAYS


TODAY = date(2026,10,28)


def package():
    return dict(version=1,mode='DEMO',price_source='DEMO',available_through=DAYS[-1],
        calendar=dict(days=list(DAYS),begin=DAYS[0],end=DAYS[-1],source='DEMO'),
        prices=[dict(day=day,code='900001',market='KOSPI',close=1000+i*10) for i,day in enumerate(DAYS)])


def write(tmp_path,data):
    path=tmp_path/'prices.json'
    path.write_text(json.dumps(data),encoding='utf8')
    return path


def test_import_updates_all_covered_recommendations_without_inventing_missing_prices(tmp_path):
    db,_=store(tmp_path)
    path=write(tmp_path,package())
    first=import_package(db,path,'DEMO',today=TODAY)
    assert first['recommendations']==3 and first['added']==3
    assert import_package(db,path,'DEMO',today=TODAY)['added']==0
    text=db.recommendation_summary('DEMO',performance=True)
    assert '+1.00%' in text and '가격 자료 없음' in text and '900002' in text


@pytest.mark.parametrize('damage',['mode','official','secret','duplicate','negative','boolean','calendar','future','size'])
def test_invalid_package_is_rejected_before_any_evaluation_write(tmp_path,damage):
    db,_=store(tmp_path)
    data=package()
    if damage=='mode': data['mode']='MOCK'
    elif damage=='official': data['calendar']['source']='OFFICIAL'
    elif damage=='secret': data['private_field']='PRIVATE'
    elif damage=='duplicate': data['prices'].append(deepcopy(data['prices'][0]))
    elif damage=='negative': data['prices'][0]['close']=-1
    elif damage=='boolean': data['prices'][0]['close']=True
    elif damage=='calendar': data['calendar']['days'].remove(DAYS[1])
    elif damage=='future': data['available_through']='2026-10-29';data['calendar']['end']='2026-10-29'
    path=write(tmp_path,data)
    if damage=='size': path.write_bytes(b' '*2_000_001)
    with pytest.raises(ValueError): import_package(db,path,'DEMO',today=TODAY)
    with db.connect() as record:
        assert record.execute('SELECT COUNT(*) FROM recommendation_reviews').fetchone()[0]==0


def test_real_mode_user_package_does_not_claim_official_provenance(tmp_path):
    db,_=store(tmp_path)
    with db.connect() as record:
        record.execute("UPDATE recommendation_batches SET mode='MOCK',source='KIWOOM'")
    data=package();data.update(mode='MOCK',price_source='CSV_USER');data['calendar']['source']='USER_CONFIRMED'
    path=write(tmp_path,data)
    assert import_package(db,path,'MOCK',today=TODAY)['added']==3
    with db.connect() as record:
        row=record.execute('SELECT price_source,calendar_source FROM recommendation_reviews').fetchone()
        assert tuple(row)==('CSV_USER','USER_CONFIRMED')
    assert '저장된 추천이 없어요' in db.recommendation_summary('DEMO')


def test_unparseable_file_does_not_echo_private_content(tmp_path):
    path=tmp_path/'prices.json';path.write_text('PRIVATE invalid json',encoding='utf8')
    with pytest.raises(ValueError) as error: load_package(path,'DEMO',today=TODAY)
    assert 'PRIVATE' not in str(error.value)


def test_failure_halfway_through_package_rolls_back_all_new_reviews(tmp_path,monkeypatch):
    import app.recommendation_import as module
    db,_=store(tmp_path)
    path=write(tmp_path,package())
    original=module.review
    count=[]
    def fail_second(*args,**kwargs):
        count.append(1)
        if len(count)==2: raise ValueError('시험 중단')
        return original(*args,**kwargs)
    monkeypatch.setattr(module,'review',fail_second)
    with pytest.raises(ValueError,match='시험 중단'):
        import_package(db,path,'DEMO',today=TODAY)
    with db.connect() as record:
        assert record.execute('SELECT COUNT(*) FROM recommendation_reviews').fetchone()[0]==0
        assert record.execute('SELECT COUNT(*) FROM recommendations').fetchone()[0]==3


def test_date_filter_uses_korean_date_even_for_timezone_offset_record(tmp_path):
    db,_=store(tmp_path)
    # 18th UTC is 19th in Korea; never drop it by comparing string prefixes.
    with db.connect() as record:
        record.execute('UPDATE recommendation_batches SET created_at=?',('2026-10-18T16:00:00+00:00',))
    assert import_package(db,write(tmp_path,package()),'DEMO',today=TODAY)['recommendations']==3


def test_out_of_range_or_naive_time_records_are_not_silently_repaired(tmp_path):
    db,_=store(tmp_path)
    with db.connect() as record:
        record.execute('UPDATE recommendation_batches SET created_at=?',('2026-10-19T10:00:00',))
    with pytest.raises(ValueError,match='추천이 없어요'):
        import_package(db,write(tmp_path,package()),'DEMO',today=TODAY)


@pytest.mark.parametrize('field',['available_through','price_day','calendar_day'])
def test_compact_date_input_is_rejected_before_review_writes(tmp_path,field):
    db,_=store(tmp_path);data=package()
    if field=='available_through': data['available_through']=data['available_through'].replace('-','')
    elif field=='price_day': data['prices'][1]['day']=data['prices'][1]['day'].replace('-','')
    else: data['calendar']['days'][1]=data['calendar']['days'][1].replace('-','')
    with pytest.raises(ValueError): import_package(db,write(tmp_path,data),'DEMO',today=TODAY)
    with db.connect() as record:
        assert record.execute('SELECT COUNT(*) FROM recommendation_reviews').fetchone()[0]==0
