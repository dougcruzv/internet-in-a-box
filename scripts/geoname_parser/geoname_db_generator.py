#!/usr/bin/env python

from optparse import OptionParser
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import Column, String, Integer, create_engine, inspect
from sqlalchemy.orm import sessionmaker
import codecs
import geoname_org_model as dbmodel
import dbhelper

def main(dbfilename, build_info, build_names):
    """
    :param dbfilename: SQLite database filename to create
    :param build_info: bool flag to indicate whether to populate place info table
    :param build_names: bool flag to indicate whether to populate place names table
    """
    aux_data = load_lookup_tables()

    database = dbhelper.Database(dbmodel.Base, dbfilename)
    database.create()
    session = database.get_session()

    if build_info or build_names:
        print 'dropping index...'
        dbmodel.drop_indices(session)

    if build_names:
        print 'parse names...'
        parse_alt_names_to_db(session)

    if build_info:
        print 'parse places...'
        parse_place_info_to_db(session, aux_data)

    if build_info or build_names:
        print 'creating index...'
        dbmodel.create_indices(session)

def load_lookup_tables():
    aux_data = {}
    aux_data['admin1'] = build_dictionary('admin1CodesASCII.txt', dbmodel.admin1_fields, 'code')
    aux_data['admin2'] = build_dictionary('admin2Codes.txt', dbmodel.admin2_fields, 'code')
    aux_data['features'] = build_dictionary('featureCodes_en.txt', dbmodel.feature_fields, 'code')
    aux_data['countries'] = build_dictionary('countryInfo.txt', dbmodel.country_fields, 'iso')
    aux_data['cities'] = build_dictionary('cities1000.txt', dbmodel.place_fields, 'id')
    return aux_data

def parse_alt_names_to_db(dbSession):
    """Create names data"""
    for count, record in enumerate(record_iterator('alternateNames.txt', dbmodel.altname_fields)):
        pn = dbmodel.PlaceNames(**record)
        dbSession.add(pn)
        # without an occasional commit we were running out of memory
        if (count & 0x7ffff) == 0:
            dbSession.commit()
            print '.',
    dbSession.commit()

def parse_place_info_to_db(dbSession, aux_data):
    """Create places database while denormalizing smaller geonames source tables with aux_data"""
    dbSession.query(dbmodel.PlaceInfo).delete()

    # create names data
    for count, record in enumerate(record_iterator('allCountries.txt', dbmodel.place_fields)):
        #augment record
        record = augment_record(aux_data, record)
        pi = make_place_info(aux_data, record)
        dbSession.add(pi)
        # without an occasional commit we were running out of memory
        if (count & 0x7ffff) == 0:
            dbSession.commit()
            print '.',
    dbSession.commit()

def record_iterator(filename, field_names):
    """Iterate over source text-data files

    Parse tab delimited files, skipping lines starting with #, yielding on each record in the file.
    Return a dict of values tagged with keys provided in field_names list.
    """
    with codecs.open(filename, encoding='utf-8') as f:
        for line in f:
            if line.lstrip().startswith('#'):
                continue
            yield dict(zip(field_names, map(unicode.strip, line.split('\t'))))


def build_dictionary(filename, fields, key):
    """Parse records in filename into a dictionary map indexed by field `key`

    :param filename: filename of text source file containing records to parse
    :param fields: list of field names identifying delimited field in each record
    :param key: string name of a field in `fields` which should be used as the key in resulting record map
    :return: dictionary of fields indexed using the field named in `key`
    """
    assert key in fields

    results = {}
    for r in record_iterator(filename, fields):
        if r[key] != '':
            results[r[key]] = r
        else:
            print "Skipping record with missing " + key + ": " + r

    return results

def builddb(db, insp, descriptor):
    filename, fields, cls = descriptor

    if cls is None:
        print "skipping " + filename
        return

    db_columns = [d['name'] for d in insp.get_columns(cls.__tablename__)]
    print db_columns

    print "Working on " + filename
    count = 0
    for r in record_iterator(filename, fields):
        filtered_data = {k : r[k] for k in r if k in db_columns}
        data = cls(**filtered_data)
        db.add(data)
        count += 1
        if (count % 100000) == 0:
            print count
            db.commit()
    db.commit()

def make_place_info(aux_data, rec):
    data = {}

    direct_copy_fields = ('id', 'latitude', 'longitude', 'population', 'feature_code', 'name', 'asciiname')
    for f in direct_copy_fields:
        data[f] = rec[f]

    # Probably need lookup table for admin4/admin3 codes conversion to geoid.
    data['admin4_id'] = rec['admin4_code']
    data['admin3_id'] = rec['admin3_code']
    data['admin2_id'] = place_admin2_id(aux_data, rec)
    data['admin1_id'] = place_admin1_id(aux_data, rec)
    data['country_id'] = place_country_id(aux_data, rec)
    data['feature_name'] = place_feature_name(aux_data, rec)
    try_for_improved_population_estimate(aux_data, data)
    return dbmodel.PlaceInfo(**data)

def augment_record(aux_data, record):
    """For entities with populations insert the largest population found into the record"""
    for auxkey in ('cities', 'countries'):
        geoid = record['id']
        if geoid in aux_data[auxkey]:
            population = aux_data[auxkey][geoid]['population']
            if population > record['population']:
                record['population'] = population
    return record

def try_for_improved_population_estimate(aux_data, data):
    """Populate place record `data` with population estimate from aux_data if any"""
    def update_pop(geoid, rec):
        pop = rec['population']
        if data['population'] != pop:
            if data['population'] != 0:
                print key + " population mismatch on %d: %d %d" % (
                        geoid, data['population'], pop)
            data['population'] = pop

    geoid = data['id']
    if geoid in aux_data['cities']:
        rec = aux_data['cities'][geoid]
        update_pop(geoid, rec)
    elif geoid in aux_data['countries']:
        rec = aux_data['countries'][geoid]
        update_pop(geoid, rec)

def place_admin1_id(aux_data, rec):
    """Return geoid associated with records admin1 place code.  Returns '' if not found"""
    # bear in mind that the admin1 code 00 means no specific admin1 code is defined
    country_code = rec['country_code']
    admin1_code = rec['admin1_code']

    if country_code == '' or admin1_code == '':
        return ''
    admin1key = "%s.%s" % (country_code, admin1_code)
    try:
        return aux_data['admin1'][admin1key]['id']
    except KeyError:
        print u''.join((u"Failed to find admin1 code for ", admin1key, u" on ", rec['id'])).encode('utf-8')
        return ''

def place_admin2_id(aux_data, rec):
    """Return geoid associated with records admin2 place code.  Returns '' if not found"""
    # bear in mind that the admin1 code 00 means no specific admin1 code is defined
    country_code = rec['country_code']
    admin1_code = rec['admin1_code']
    admin2_code = rec['admin2_code']

    if admin1_code == '' or admin2_code == '' or country_code == '':
        return ''
    admin2key = "%s.%s.%s" % (country_code, admin1_code, admin2_code)
    try:
        return aux_data['admin2'][admin2key]['id']
    except KeyError:
        print u''.join((u"Failed to find admin2 code for ", admin2key, u" on ", rec['id'])).encode('utf-8')
        return ''

def place_country_id(aux_data, rec):
    """Return geoid associated with records country_code.  Returns '' if not found"""
    countrycode = rec['country_code']
    if countrycode == '':
        return ''

    try:
        return aux_data['countries'][countrycode]['id']
    except KeyError:
        print u''.join((u"Failed to find country code for ", countrycode, u" on ", rec['id'])).encode('utf-8')
        return ''

def place_feature_name(aux_data, rec):
    featureclass = rec['feature_class']
    featurecode = rec['feature_code']

    if featureclass == '' or featurecode == '':
        return ''

    featurekey = "%s.%s" % (featureclass, featurecode)
    try:
        return aux_data['features'][featurekey]['name']
    except KeyError:
        print u''.join((u"Failed to find feature code for ", featurekey, u" on ", rec['id'])).encode('utf-8')
        return ''


    
if __name__ == '__main__':
    parser = OptionParser(description="Parse geonames.org geo data into a SQLite DB.")
    parser.add_option("--dbname", dest="db_filename", action="store",
                      default="geodata2.db",
                      help="The geodata.db SQLite database")
    parser.add_option("--srcdir", dest="data_dir", action="store",
                      default="",
                      help="Specify directory in which data files can be found")
    parser.add_option("--disable-info", action="store_false", dest="build_info", default=True)
    parser.add_option("--disable-names", action="store_false", dest="build_names", default=True)

    (options, args) = parser.parse_args()

    main(options.db_filename, options.build_info, options.build_names)


