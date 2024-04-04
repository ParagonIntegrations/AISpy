import sys
sys.path.append('/opt/aispy/aispy')
from app import FractalApp

if __name__ == '__main__':
	app = FractalApp()
	app.run()