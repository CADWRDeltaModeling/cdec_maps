# %%
from cdec_maps import cdec
from cdec_maps import panels

# %%
reader = cdec.Reader()

# %%
stations = reader.read_all_stations()
# %%
stations_meta_info = reader.read_all_stations_meta_info()
# %%
stations_joined_info = stations_meta_info
# %%
plotter = panels.CDECPlotterAllSingleStation(stations, stations_joined_info)

# %%
plotter.get_panel().servable()
# %%
