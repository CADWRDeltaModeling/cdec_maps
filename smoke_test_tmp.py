from cdec_maps.cdecuimgr import CDECDataReference, CDECDataReferenceReader
ref = CDECDataReference(
    name='CAP/1/H',
    ID='CAP',
    **{'Sensor Number': '1', 'duration_code': 'H', 'Sensor': 'RIV STG', 'Units': 'FT', 'Duration': '(hourly)'}
)
print('station_id:', ref.station_id)
print('sensor_number:', ref.sensor_number)
print('duration_code:', ref.duration_code)
print('unit:', ref.unit)
print('sensor:', ref.sensor)
r = CDECDataReferenceReader()
print('reader repr:', repr(r))
print('All smoke tests PASSED')
