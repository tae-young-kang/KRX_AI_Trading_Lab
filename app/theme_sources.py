KIWOOM_SOURCES={'MOCK':'KIWOOM_MOCK_KRX','REAL_QUOTES':'KIWOOM_REAL_KRX'}


def valid_source(mode,source):
    if mode=='DEMO':return source=='DEMO'
    return mode in KIWOOM_SOURCES and source in ('KRX','CSV_USER',KIWOOM_SOURCES[mode])
