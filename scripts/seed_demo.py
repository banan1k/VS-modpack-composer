import asyncio
import json
from app.db import SessionLocal, init_db
from app.models import Mod, ModRelease, Dependency, Compatibility

async def main():
    await init_db()
    async with SessionLocal() as s:
        a=Mod(name="Common Library", mod_id="commonlib", description="Demo library", latest_game_version="1.22.7", supported_versions_json=json.dumps(["1.22.7"]), latest_file_name="commonlib-demo.zip", latest_file_url="https://example.invalid/commonlib.zip")
        b=Mod(name="Example Adventure", mod_id="exampleadventure", description="Demo content mod", latest_game_version="1.22.7", supported_versions_json=json.dumps(["1.22.7"]), latest_file_name="exampleadventure-demo.zip", latest_file_url="https://example.invalid/exampleadventure.zip")
        c=Mod(name="Example Conflict", mod_id="exampleconflict", description="Demo conflict", latest_game_version="1.22.7", supported_versions_json=json.dumps(["1.22.7"]), latest_file_name="exampleconflict-demo.zip", latest_file_url="https://example.invalid/exampleconflict.zip")
        s.add_all([a,b,c]); await s.flush()
        s.add(Dependency(mod_id=b.id,target_mod_id=a.id,target_name=a.name,relation_type="dependency",confidence=.98,evidence="Depends on Common Library",verified=False))
        s.add(Compatibility(mod_id=b.id,target_mod_id=c.id,target_name=c.name,relation_type="incompatible",confidence=.9,evidence="Incompatible with Example Conflict",verified=False))
        s.add_all([ModRelease(mod_id=a.id,release_id=1,mod_version="1.0.0",filename=a.latest_file_name,file_url=a.latest_file_url,game_versions_json=json.dumps(["1.22.7"])),ModRelease(mod_id=b.id,release_id=2,mod_version="2.0.0",filename=b.latest_file_name,file_url=b.latest_file_url,game_versions_json=json.dumps(["1.22.7"])),ModRelease(mod_id=c.id,release_id=3,mod_version="1.0.0",filename=c.latest_file_name,file_url=c.latest_file_url,game_versions_json=json.dumps(["1.22.7"]))])
        await s.commit()
    print("Demo catalog seeded.")

asyncio.run(main())
