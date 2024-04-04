import sys
print(sys.path)
sys.path.append('/opt/aispy/aispy')
from app import AISpyApp, FractalApp

if __name__ == '__main__':
	# app = AISpyApp()
	# app.start()
	app = FractalApp()
	app.run()