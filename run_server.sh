git config --global --add safe.directory /home/data/cdec-maps
pip install git+https://github.com/CADWRDeltaModeling/vtools3
SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 pip install --no-deps .
#pip install panel==1.5.0b3 # FIXME: remove this line when panel 1.5.0 is released
#python cdec_maps/cdec_cache_build.py &
panel serve cdec.py --address 0.0.0.0 --port 80 --allow-websocket-origin="*"
