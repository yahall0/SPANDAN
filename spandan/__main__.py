import os

import uvicorn

if __name__ == '__main__':
    uvicorn.run('spandan.app:app', host='127.0.0.1', port=int(os.environ.get('SPANDAN_PORT', '8765')),
                workers=1, proxy_headers=False, access_log=False)
