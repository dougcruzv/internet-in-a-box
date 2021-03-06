#!/usr/bin/env python

# Expand the import search path to allow inclusion of profiling code
import sys
import os
import logging
package_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, package_dir)

from optparse import OptionParser
import geoname_org_model as gndata
import iiab_maps_model as ibdata
import dbhelper
from unicodedata2 import script_cat # helper script from local directory
from sqlalchemy import inspect

import iiab.timepro as timepro

log = logging.getLogger("iiab_db_generator")

"""
Read geoname.org data that has been placed into a SQLite database and create
a new database consisting of full name records structured for efficient autocomplete
and geolookup.
"""

# storage for stats collection since not organized as a class...
_stats = {}

def main(geoname_db_filename, iiab_db_filename):
    sepDb = dbhelper.Database(gndata.Base, geoname_db_filename)

    uniDb = dbhelper.Database(ibdata.Base, iiab_db_filename)
    uniDb.clear_table(ibdata.GeoNames)
    uniDb.clear_table(ibdata.GeoInfo)
    uniDb.clear_table(ibdata.GeoLinks)

    uniDb.create()
    ibdata.drop_indices(uniDb.get_session())

    work(sepDb.get_session(), uniDb.get_session())

    ibdata.create_indices(uniDb.get_session())
    show_stats()

    log.info("Removing redundant entries.")
    remove_duplicate_rows(uniDb.get_session())

    timepro.log_all();

def work(insession, outsession):
    # for each place, inspect all of the different names.
    BLK_SIZE = 1000
    for count, v in enumerate(insession.query(gndata.PlaceInfo).yield_per(BLK_SIZE).enable_eagerloads(False)):
        timepro.start("convert PlaceInfo")
        id_list = (v.id, v.admin4_id, v.admin3_id, v.admin2_id, v.admin1_id, v.country_id)
        (name_records, links) = get_namesets(insession, id_list)

        # insert geoinfo record
        info = ibdata.GeoInfo(id=v.id, latitude=v.latitude, longitude=v.longitude, population=v.population,
                feature_code=v.feature_code, feature_name=v.feature_name)
        outsession.add(info)

        # insert into geolinks
        for l in links:
            outsession.add(ibdata.GeoLinks(geoid=v.id, link=l))

        # Not all places have alternate name records -- use them if we do, otherwise fallback to name in the placeinfo table
        timepro.start("add GeoNames")
        if v.id in name_records:
            for record in name_records[v.id]:
                expanded_name = get_expanded_name(name_records, record, id_list)

                place = ibdata.GeoNames(geoid=v.id, lang=record.isolanguage, name=record.alternate, fullname=expanded_name, importance=v.population)
                outsession.add(place)

                #print v.id, record.isolanguage, expanded_name, v.feature_name, v.population
        timepro.end("add GeoNames")

        # now expand name found in the placeinfo table.
        (name, expanded_name) = get_expanded_info_name(name_records, id_list)
        place = ibdata.GeoNames(geoid=v.id, lang='en', name=name, fullname=expanded_name, importance=v.population)
        outsession.add(place)

        # now expand asciiname found in the placeinfo table.
        (name, expanded_name) = get_expanded_info_asciiname(name_records, id_list)
        place = ibdata.GeoNames(geoid=v.id, lang='en', name=name, fullname=expanded_name, importance=v.population)
        outsession.add(place)
#        print expanded_name

        if (count & 0x1ff) == 0:
            log.info("progress: %d" % count)
            outsession.commit()

        timepro.end("convert PlaceInfo")

    outsession.commit()

@timepro.profile()
def get_namesets(session, idlist):
    """Compile all name variations and links associated with a geoid

    Return a dictionary of all the names that will be used to construct the
    fully expanded place name Indexes names in a variety of ways. Note
    carefully that two different value types are stored.  The value type is a
    PlaceNames record except when the key is (id, '__infoname__') when the
    value is a PlaceInfo record.

    :param session: database session for querying the allCountries and alternatenames content
    :param idlist: ordered list of geographic IDs, administrative area ID codes, and country ID code.
    """
    geoid = idlist[0] # first list element is the specific place we are describing
    lookup = {}
    links = []  # links (just for geoid)

    # store PlaceNames records
    for v in session.query(gndata.PlaceNames).filter(gndata.PlaceNames.geonameid.in_(idlist)).all():
        if not v.isolanguage:
            if v.geonameid == geoid:
                stats('isolanguage empty')
            continue

        if v.isolanguage == u'link':
            if v.geonameid == geoid:
                links.append(v.alternate)
            continue

        value_tup = v
        # key tuples selected to make fallback matches easy
        add(lookup, v.geonameid, value_tup)   # key:int
        add(lookup, (v.geonameid, v.isolanguage), value_tup) # key:int,str
        add(lookup, (v.geonameid, v.isPreferredName), value_tup) # key:int,bool
        add(lookup, (v.geonameid, v.isolanguage, v.isPreferredName, v.isShortName, v.isColloquial, v.isHistoric), value_tup) # key:int,str,bool,bool,bool,bool
        add(lookup, (v.geonameid, v.isolanguage, v.isPreferredName, v.isHistoric), value_tup) # key:int,str,bool,bool
        add(lookup, (v.geonameid, v.isolanguage, v.isPreferredName), value_tup) # key:int,str,bool

    # store PlaceInfo records
    for v in session.query(gndata.PlaceInfo).filter(gndata.PlaceInfo.id.in_(idlist)).all():
        add(lookup, (v.id, "__infoname__"), v)

    return (lookup, links)

def append_if_not_empty(namelist, name):
    """Helper function for building full expanded name"""
    if name is not None:
        namelist.append(name)

def build_script_tally(name):
    """Build a frequency tally of script types used in the name"""
    scripts = {}
    for c in name:
        scriptname = script_cat(c)
        if scriptname in scripts:
            scripts[scriptname] += 1
        else:
            scripts[scriptname] = 1
    return scripts

def match_script(refname, candidatematches):
    """Find name that best matches the requested unicode script

    Return tuple consisting of (single name record with best match, int number
    of records that matched, bool true if some script in common, bool true if
    matched predominant script)

    :param refname: name record we are matching
    :param candidatematches: list of name records to search
    """
    # find the predominent script used in each candidate name. then create a dict with script name as
    # as the key and the list of places as the value
    candidatescripts = {} # key: script name, value: list of geographic names
    for namerec in candidatematches:
        try: # Might be best to profile compared to test for attribute. Primary case is a name record.
            name = namerec.alternate
        except AttributeError:
            name = namerec.name
        tally = build_script_tally(name)  # build the script frequency table
        ordered_tally = sorted(tally.iteritems(), key=lambda t: t[1], reverse=True) # order most to least
        (script_type, _) = ordered_tally[0]    # pick the most prevalent script type (ties ignored)
        add(candidatescripts, script_type, namerec)  # index based on most prevalent script

    # build a frequency table of script types used in the name
    refscripts = build_script_tally(refname)
    ordered_reftally = sorted(refscripts.iteritems(), key=lambda t: t[1], reverse=True) # ordered most to least
    best_match = True
    # step through featured place name scripts looking for a candidate with a predominantly matching script
    for (script_type, _) in ordered_reftally:
        if script_type in candidatescripts:  # we found a match!
            number_of_matches = len(candidatescripts[script_type])
            perfect_match = True
            namerec = candidatescripts[script_type][0] # arbitrarily select first name record in list
            return (namerec, number_of_matches, perfect_match, best_match)
        best_match = False

    number_of_matches = 1
    perfect_match = False
    best_match = False
    name = candidatematches[0] # just take first entry
    return (name, number_of_matches, perfect_match, best_match)

@timepro.profile()
def get_closest_match(records, rec, gid):
    """Return a geographic container name for gid that has similar language and
    type flags as the rec name

    :param records: dictionary of namesets obtained from get_nameset
    :param rec: specific PlaceNames record whose containing place name we are searching for
    :param gid: the place id of the containing geography whose name we seek
    :return: unicode string of best match found
    """
    # key tuples must match tuple keys in lookup table
    # order in priority rank
    prioritized_keys = [
        (gid, rec.isolanguage, rec.isPreferredName, rec.isShortName, rec.isColloquial, rec.isHistoric),
        (gid, rec.isolanguage, rec.isPreferredName, rec.isHistoric),
        (gid, rec.isolanguage, rec.isPreferredName),
        (gid, rec.isolanguage, 1),
        (gid, rec.isolanguage),
        (gid, 1),  # just pick a preferred name from any language -- best we can do really?
        (gid, "__infoname__") # okay no alternate name match -- fallback to PlaceTable names
        ]

    for key in prioritized_keys:
        if key in records:
            # try to pic a record that uses the same unicode script
            (matched, number_of_matches, perfectmatch, best_match) = match_script(rec.alternate, records[key])
            if number_of_matches != 1:
                stats('multiple name types match', extra=(key, number_of_matches))
            if not best_match:
                stats('chose partially matched script', extra=key)
            if not perfectmatch:
                stats('fallback; no matching script', extra=key)

            if key[1] == '__infoname__':
                stats('fallback to matching infotable')
                return matched.name
            else:
                #print "matched record", key
                return matched.alternate


def get_expanded_name(records, rec, id_list):
    """
    Return a unicode string with fully expanded name for place described by PlaceName record rec
    :param records: dictionary of namesets obtained from get_nameset
    :param rec: specific PlaceNames record whose containing place name we are searching for
    :param id_list: list of geographic ID codes ordered smallest to largest.
    """
    name = [rec.alternate]
    # id_list is ordered smallest to largest geographic area
    for idcode in id_list[1:]:  # skip rec ID which is first element in id_list
        append_if_not_empty(name, get_closest_match(records, rec, idcode))
    return u', '.join(name)

def get_expanded_info_name(records, id_list):
    """
    Return a unicode string with fully expanded name for place using PlaceInfo.name values
    :param records: dictionary of namesets obtained from get_nameset
    :param id_list: list of geographic ID codes ordered smallest to largest.
    """
    fullname_parts = []
    for idcode in id_list:
        key = (idcode, "__infoname__")
        if key in records:
            info_rec = records[key][0]  # there should only be one geoid since primary key of GeoInfo
            append_if_not_empty(fullname_parts, info_rec.name)
    return (fullname_parts[0], u', '.join(fullname_parts))

def get_expanded_info_asciiname(records, id_list):
    """
    Return a unicode string with fully expanded name for place using PlaceInfo.asciiname values
    :param records: dictionary of namesets obtained from get_nameset
    :param id_list: list of geographic ID codes ordered smallest to largest.
    """
    fullname_parts = []
    for idcode in id_list:  # skip primary place ID which is first element
        key = (idcode, "__infoname__")
        if key in records:
            info_rec = records[key][0]  # there should only be one geoid since primary key of GeoInfo
            append_if_not_empty(fullname_parts, info_rec.asciiname)
    return (fullname_parts[0], u', '.join(fullname_parts))

def add(lookup, key, value):
    """
    Helper function for creating/adding values to a dict of lists
    :param lookup: dictionary
    :param key: dict key
    :param value: new value to add to list in dict
    """
    lookup.setdefault(key, []).append(value)

def stats(tag, extra=''):
    global _stats
    if tag not in _stats:
        _stats[tag] = {}
    if extra in _stats[tag]:
        _stats[tag][extra] += 1
    else:
        _stats[tag][extra] = 1

def show_stats():
    print _stats


def remove_duplicate_rows(session):
    columns = inspect(ibdata.GeoNames).columns.keys()
    columns_without_id = filter(lambda r: r != 'id', columns)
    count = session.query(ibdata.GeoNames).count()
    log.info("Count before pruning: %d" % count)

    # courtesy of https://stackoverflow.com/a/18949
    table_name = ibdata.GeoNames.__tablename__
    session.execute('DELETE FROM %s WHERE id NOT IN (SELECT MIN(id) FROM %s GROUP BY %s)' % (table_name, table_name, ', '.join(columns_without_id)))
    session.commit()

    count = session.query(ibdata.GeoNames).count()
    log.info("Count after pruning: %d" % count)


if __name__ == '__main__':
    parser = OptionParser(description="Parse geonames.org geo data database to create the geodata db IIAB will use.")
    parser.add_option("--in", dest="db_filename", action="store",
                      default="geoname_geodata.db",
                      help="The geonamegeodata.db SQLite database to be used as input")
    parser.add_option("--out", dest="out_db_filename", action="store",
                      default="iiab_geodata.db",
                      help="The iiabgeodata.db SQLite database to create")

    (options, args) = parser.parse_args()

    main(options.db_filename, options.out_db_filename)


